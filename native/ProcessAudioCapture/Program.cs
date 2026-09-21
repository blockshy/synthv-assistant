using System.Diagnostics;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;

namespace ProcessAudioCapture;

/// <summary>
/// 基于 Windows 按进程回录接口的有限时长录音器。仅采集指定进程及其子进程的输出，
/// 不打开麦克风，不降级为系统混音录音。接口声明依据微软公开 WASAPI ABI。
/// </summary>
internal static class Program
{
    private const uint SampleRate = 44100;
    private const ushort Channels = 2;
    private const ushort BlockAlign = 4;

    [MTAThread]
    private static int Main(string[] args)
    {
        Console.OutputEncoding = new UTF8Encoding(false);
        Console.InputEncoding = new UTF8Encoding(false);
        bool comInitialized = false;
        try
        {
            var options = Options.Parse(args);
            if (!OperatingSystem.IsWindowsVersionAtLeast(10, 0, 20348))
                throw new PlatformNotSupportedException("按进程回录要求 Windows build 20348 或更高版本。");
            // 在首次 P/Invoke 前显式建立 MTA；S_FALSE 也代表成功，必须配对 CoUninitialize。
            Native.Check(Native.CoInitializeEx(IntPtr.Zero, 0));
            comInitialized = true;
            using var targetProcess = Process.GetProcessById(options.Pid);
            if (targetProcess.HasExited) throw new InvalidOperationException("目标进程已退出。");
            Capture(options);
            return 0;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine($"音频采集失败：{error.Message}（0x{error.HResult:X8}）");
            return 1;
        }
        finally
        {
            if (comInitialized) Native.CoUninitialize();
        }
    }

    private static void Capture(Options options)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(options.Output)!);
        Directory.CreateDirectory(Path.GetDirectoryName(options.ReadyFile)!);
        if (File.Exists(options.Output) || File.Exists(options.ReadyFile))
            throw new IOException("输出文件或就绪标记已存在，请使用新的文件名。");

        // VT_BLOB 的数据指向 AUDIOCLIENT_ACTIVATION_PARAMS；包含进程树模式的枚举值为 0。
        var activation = new ActivationParameters { ActivationType = 1, TargetProcessId = (uint)options.Pid, ProcessLoopbackMode = 0 };
        IntPtr activationMemory = Marshal.AllocHGlobal(Marshal.SizeOf<ActivationParameters>());
        IActivateAudioInterfaceAsyncOperation? operation = null;
        IAudioClient? audioClient = null;
        IAudioCaptureClient? captureClient = null;
        var callback = new ActivationCallback();
        using var bufferReady = new EventWaitHandle(false, EventResetMode.AutoReset);
        bool started = false;
        try
        {
            Marshal.StructureToPtr(activation, activationMemory, false);
            var parameters = new PropVariant
            {
                VariantType = 65, // VT_BLOB。
                BlobSize = (uint)Marshal.SizeOf<ActivationParameters>(),
                BlobData = activationMemory
            };
            var audioClientId = typeof(IAudioClient).GUID;
            Native.Check(Native.ActivateAudioInterfaceAsync("VAD\\Process_Loopback", ref audioClientId, ref parameters, callback, out operation));
            // 异步 COM 激活失败必须限时退出，否则上层无法可靠地决定是否开始 SynthV 播放。
            if (!callback.Completed.Wait(TimeSpan.FromSeconds(8)))
                throw new TimeoutException("Windows 在 8 秒内未完成按进程音频接口激活。");
            if (callback.Error is not null) throw callback.Error;
            audioClient = callback.AudioClient ?? throw new InvalidOperationException("Windows 未返回音频客户端。");

            var format = new WaveFormat
            {
                FormatTag = 1, Channels = Channels, SamplesPerSecond = SampleRate,
                AverageBytesPerSecond = SampleRate * BlockAlign, BlockAlign = BlockAlign,
                BitsPerSample = 16, ExtraSize = 0
            };
            // LOOPBACK | EVENTCALLBACK | AUTOCONVERTPCM，与微软 ApplicationLoopback 示例一致。
            Native.Check(audioClient.Initialize(0, 0x00020000 | 0x00040000 | 0x80000000, 0, 0, ref format, IntPtr.Zero));
            var captureId = typeof(IAudioCaptureClient).GUID;
            Native.Check(audioClient.GetService(ref captureId, out var captureObject));
            captureClient = (IAudioCaptureClient)captureObject;
            Native.Check(audioClient.SetEventHandle(bufferReady.SafeWaitHandle.DangerousGetHandle()));

            int totalFrames = checked((int)Math.Round(options.Seconds * SampleRate));
            byte[] pcm = new byte[checked(totalFrames * BlockAlign)];
            long receivedFrames = 0;
            long nextFrame = 0;
            int discontinuities = 0;
            int timestampErrors = 0;
            DateTimeOffset startedAt = DateTimeOffset.UtcNow;
            long startTicks = Stopwatch.GetTimestamp();
            double startHundredNanoseconds = startTicks * (10000000.0 / Stopwatch.Frequency);
            Native.Check(audioClient.Start());
            started = true;

            // Start 成功后再原子发布 ready-file。Python 看到该标记即可发起 SynthV 播放。
            var ready = new
            {
                status = "ready", pid = options.Pid, scope = "process_tree",
                started_at_utc = startedAt.ToString("O"), sample_rate = SampleRate,
                channels = Channels, bits_per_sample = 16, duration_seconds = options.Seconds,
                output = options.Output
            };
            AtomicJson(options.ReadyFile, ready);

            void DrainPackets()
            {
                Native.Check(captureClient.GetNextPacketSize(out uint packetSize));
                while (packetSize > 0)
                {
                    Native.Check(captureClient.GetBuffer(out IntPtr data, out uint frames, out uint flags, out _, out ulong qpcTime));
                    try
                    {
                        receivedFrames += frames;
                        if ((flags & 1) != 0) discontinuities++;
                        bool validTimestamp = (flags & 4) == 0 && qpcTime != 0;
                        if (!validTimestamp) timestampErrors++;
                        // QPC 包时间戳按 100 ns 计数；按墙钟放置样本，保留播放前的等待与中途静音。
                        long firstFrame = validTimestamp
                            ? (long)Math.Round(((double)qpcTime - startHundredNanoseconds) * SampleRate / 10000000.0)
                            : nextFrame;
                        long skippedFrames = Math.Max(0, -firstFrame);
                        long destinationFrame = Math.Max(0, firstFrame);
                        long copiedFrames = Math.Max(0, Math.Min((long)frames - skippedFrames, totalFrames - destinationFrame));
                        if (copiedFrames > 0 && (flags & 2) == 0 && data != IntPtr.Zero)
                        {
                            Marshal.Copy(IntPtr.Add(data, checked((int)(skippedFrames * BlockAlign))), pcm,
                                checked((int)(destinationFrame * BlockAlign)), checked((int)(copiedFrames * BlockAlign)));
                        }
                        nextFrame = Math.Max(nextFrame, firstFrame + frames);
                    }
                    finally
                    {
                        Native.Check(captureClient.ReleaseBuffer(frames));
                    }
                    Native.Check(captureClient.GetNextPacketSize(out packetSize));
                }
            }

            while (Stopwatch.GetElapsedTime(startTicks).TotalSeconds < options.Seconds)
            {
                // 有限等待亦覆盖尚未输出音频的目标；不存在无限挂起的采集循环。
                var remaining = options.Seconds - Stopwatch.GetElapsedTime(startTicks).TotalSeconds;
                bufferReady.WaitOne(Math.Clamp((int)Math.Ceiling(remaining * 1000), 1, 50));
                DrainPackets();
            }
            Native.Check(audioClient.Stop());
            started = false;
            DrainPackets();
            WriteWave(options.Output, pcm);
            var result = new
            {
                status = "complete", pid = options.Pid, scope = "process_tree", output = options.Output,
                started_at_utc = startedAt.ToString("O"), ended_at_utc = DateTimeOffset.UtcNow.ToString("O"),
                duration_seconds = totalFrames / (double)SampleRate,
                capture_elapsed_seconds = Stopwatch.GetElapsedTime(startTicks).TotalSeconds,
                sample_rate = SampleRate, channels = Channels, bits_per_sample = 16,
                frames = totalFrames, received_frames = receivedFrames,
                discontinuities, timestamp_errors = timestampErrors,
                timestamp_alignment = "qpc_100ns", bytes = pcm.Length + 44
            };
            Console.WriteLine(JsonSerializer.Serialize(result));
        }
        finally
        {
            if (started && audioClient is not null) audioClient.Stop();
            if (captureClient is not null) Marshal.ReleaseComObject(captureClient);
            if (audioClient is not null) Marshal.ReleaseComObject(audioClient);
            if (operation is not null) Marshal.ReleaseComObject(operation);
            // 在异步操作结束前保持参数及回调存活，避免 COM 使用已回收的托管对象。
            GC.KeepAlive(callback);
            Marshal.FreeHGlobal(activationMemory);
        }
    }

    private static void AtomicJson(string path, object value)
    {
        string temporary = path + ".tmp";
        File.WriteAllText(temporary, JsonSerializer.Serialize(value), new UTF8Encoding(false));
        File.Move(temporary, path);
    }

    private static void WriteWave(string path, byte[] pcm)
    {
        // 标准 44 字节 PCM RIFF 头；先写临时文件，成功后才发布可分析的 WAV。
        string temporary = path + ".partial";
        using (var stream = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write))
        using (var writer = new BinaryWriter(stream, Encoding.ASCII))
        {
            writer.Write(Encoding.ASCII.GetBytes("RIFF")); writer.Write(pcm.Length + 36);
            writer.Write(Encoding.ASCII.GetBytes("WAVEfmt ")); writer.Write(16);
            writer.Write((ushort)1); writer.Write(Channels); writer.Write(SampleRate);
            writer.Write(SampleRate * BlockAlign); writer.Write(BlockAlign); writer.Write((ushort)16);
            writer.Write(Encoding.ASCII.GetBytes("data")); writer.Write(pcm.Length); writer.Write(pcm);
        }
        File.Move(temporary, path);
    }

    private sealed record Options(int Pid, double Seconds, string Output, string ReadyFile)
    {
        public static Options Parse(string[] args)
        {
            const string usage = "用法：--pid 进程号 --seconds 秒数(1..32) --output WAV路径 --ready-file JSON路径";
            if (args.Length != 8) throw new ArgumentException(usage);
            var values = new Dictionary<string, string>(StringComparer.Ordinal);
            for (int i = 0; i < args.Length; i += 2)
            {
                if (!new[] { "--pid", "--seconds", "--output", "--ready-file" }.Contains(args[i]) || !values.TryAdd(args[i], args[i + 1]))
                    throw new ArgumentException(usage);
            }
            if (!int.TryParse(values.GetValueOrDefault("--pid"), out int pid) || pid <= 0)
                throw new ArgumentException("进程号必须为正整数。");
            if (!double.TryParse(values.GetValueOrDefault("--seconds"), NumberStyles.Float, CultureInfo.InvariantCulture, out double seconds)
                || !double.IsFinite(seconds) || seconds < 1 || seconds > 32)
                throw new ArgumentException("录音时长必须介于 1 至 32 秒之间。");
            string output = Path.GetFullPath(values["--output"]);
            string readyFile = Path.GetFullPath(values["--ready-file"]);
            if (string.Equals(output, readyFile, StringComparison.OrdinalIgnoreCase))
                throw new ArgumentException("WAV 与就绪标记不能使用相同路径。");
            return new Options(pid, seconds, output, readyFile);
        }
    }
}

/// <summary>回调明确声明敏捷接口，使 Windows 能从 MTA 工作线程安全地完成异步激活。</summary>
[ComVisible(true), ClassInterface(ClassInterfaceType.None)]
public sealed class ActivationCallback : IActivateAudioInterfaceCompletionHandler, IAgileObject
{
    internal readonly ManualResetEventSlim Completed = new(false);
    internal IAudioClient? AudioClient;
    internal Exception? Error;

    public int ActivateCompleted(IActivateAudioInterfaceAsyncOperation operation)
    {
        try
        {
            Native.Check(operation.GetActivateResult(out int activationResult, out var audioInterface));
            Native.Check(activationResult);
            AudioClient = (IAudioClient)audioInterface;
        }
        catch (Exception error) { Error = error; }
        finally { Completed.Set(); }
        return 0;
    }
}

/// <summary>下列结构体必须保持 Win32 字节布局；PROPVARIANT 为 x64 的 24 字节布局。</summary>
[StructLayout(LayoutKind.Sequential)]
internal struct ActivationParameters { public int ActivationType; public uint TargetProcessId; public int ProcessLoopbackMode; }

[StructLayout(LayoutKind.Explicit, Size = 24)]
internal struct PropVariant
{
    [FieldOffset(0)] public ushort VariantType;
    [FieldOffset(8)] public uint BlobSize;
    [FieldOffset(16)] public IntPtr BlobData;
}

[StructLayout(LayoutKind.Sequential, Pack = 2)]
internal struct WaveFormat
{
    public ushort FormatTag, Channels;
    public uint SamplesPerSecond, AverageBytesPerSecond;
    public ushort BlockAlign, BitsPerSample, ExtraSize;
}

[ComVisible(true), Guid("41D949AB-9862-444A-80F6-C261334DA5EB"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IActivateAudioInterfaceCompletionHandler
{
    [PreserveSig] int ActivateCompleted(IActivateAudioInterfaceAsyncOperation operation);
}

[ComVisible(true), Guid("94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IAgileObject { }

[ComImport, Guid("72A22D78-CDE4-431D-B8CC-843A71199B6D"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IActivateAudioInterfaceAsyncOperation
{
    [PreserveSig] int GetActivateResult(out int activationResult, [MarshalAs(UnmanagedType.IUnknown)] out object activatedInterface);
}

// COM 方法必须按 SDK 中的声明顺序完整排列；未使用的方法也不能从虚表中删去。
[ComImport, Guid("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IAudioClient
{
    [PreserveSig] int Initialize(int shareMode, uint flags, long bufferDuration, long periodicity, ref WaveFormat format, IntPtr sessionGuid);
    [PreserveSig] int GetBufferSize(out uint frames);
    [PreserveSig] int GetStreamLatency(out long latency);
    [PreserveSig] int GetCurrentPadding(out uint padding);
    [PreserveSig] int IsFormatSupported(int mode, IntPtr format, out IntPtr closestMatch);
    [PreserveSig] int GetMixFormat(out IntPtr format);
    [PreserveSig] int GetDevicePeriod(out long defaultPeriod, out long minimumPeriod);
    [PreserveSig] int Start();
    [PreserveSig] int Stop();
    [PreserveSig] int Reset();
    [PreserveSig] int SetEventHandle(IntPtr eventHandle);
    [PreserveSig] int GetService(ref Guid interfaceId, [MarshalAs(UnmanagedType.IUnknown)] out object service);
}

[ComImport, Guid("C8ADBD64-E71E-48A0-A4DE-185C395CD317"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IAudioCaptureClient
{
    [PreserveSig] int GetBuffer(out IntPtr data, out uint frames, out uint flags, out ulong devicePosition, out ulong qpcPosition);
    [PreserveSig] int ReleaseBuffer(uint frames);
    [PreserveSig] int GetNextPacketSize(out uint frames);
}

internal static class Native
{
    [DllImport("Ole32.dll", ExactSpelling = true)]
    internal static extern int CoInitializeEx(IntPtr reserved, uint initializationFlags);

    [DllImport("Ole32.dll", ExactSpelling = true)]
    internal static extern void CoUninitialize();

    [DllImport("Mmdevapi.dll", ExactSpelling = true, CharSet = CharSet.Unicode)]
    internal static extern int ActivateAudioInterfaceAsync(string deviceInterfacePath, ref Guid interfaceId,
        ref PropVariant activationParameters, IActivateAudioInterfaceCompletionHandler completionHandler,
        out IActivateAudioInterfaceAsyncOperation operation);

    internal static void Check(int result)
    {
        if (result < 0) Marshal.ThrowExceptionForHR(result);
    }
}
