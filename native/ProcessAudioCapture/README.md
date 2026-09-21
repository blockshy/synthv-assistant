# 按进程音频采集器

该组件使用 Windows 官方 `ActivateAudioInterfaceAsync` / WASAPI 进程回录接口，
只接收指定进程及其子进程的播放输出。不会打开麦克风，也不会自动切换为全系统录音。

要求 Windows build 20348 或以上、.NET 10 x64 运行时；源码构建需要 .NET SDK 10。

```powershell
dotnet build native/ProcessAudioCapture -c Release
```

命令行协议：

```text
ProcessAudioCapture.exe --pid 1234 --seconds 10 --output D:\recordings\take.wav --ready-file D:\recordings\ready.json
```

底层采集时长为 1 至 32 秒，供界面中最长 30 秒的乐句预留播放准备与尾部缓冲。
辅助程序完成接口激活并启动录音后，原子写入就绪 JSON；
调用端收到该标记后才应开始播放。结束时生成标准 44100 Hz、双声道、PCM16 WAV，
在标准输出返回一个 JSON 对象。错误写入标准错误并返回非零退出码。

WAV 具有固定请求时长。数据包按 Windows QPC 时间戳对齐，未收到音频的区间保留静音。
`received_frames` 表示系统交付的音频帧，不能用作有声判定；有声检测由 Python 分析层完成。
`discontinuities`、`timestamp_errors` 用于提示录音完整性问题。ASIO 或其他绕过受支持系统路径的
输出可能无法回录，应实测；失败时返回错误，不扩大采集范围。

辅助程序不会覆盖已有 WAV 或就绪文件。Python 封装通过 `CREATE_NO_WINDOW` 隐藏控制台，
管理就绪超时、录音超时及辅助进程终止。

API 参考：

- [微软 ApplicationLoopback 示例](https://learn.microsoft.com/en-us/samples/microsoft/windows-classic-samples/applicationloopbackaudio-sample/)
- [IAudioCaptureClient::GetBuffer 时间戳定义](https://learn.microsoft.com/en-us/windows/win32/api/audioclient/nf-audioclient-iaudiocaptureclient-getbuffer)
- [ActivateAudioInterfaceAsync](https://learn.microsoft.com/en-us/windows/win32/api/mmdeviceapi/nf-mmdeviceapi-activateaudiointerfaceasync)

本项目按公开 ABI 独立实现 .NET 互操作声明，未引入第三方音频 NuGet 包。
