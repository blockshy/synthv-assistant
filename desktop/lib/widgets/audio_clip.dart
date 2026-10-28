import 'dart:async';

import 'package:audioplayers/audioplayers.dart';
import 'package:flutter/material.dart';

import '../core/api.dart';

/// 播放器边界只暴露组件所需能力，测试可替换为纯内存实现，不访问音频设备。
abstract interface class AudioClipPlayer {
  Stream<Duration> get positions;
  Stream<PlayerState> get states;
  Stream<AudioEvent> get events;
  Future<void> initialize();
  Future<void> setSource(BytesSource source);
  Future<void> resume();
  Future<void> pause();
  Future<void> seek(Duration position);
  Future<void> dispose();
}

/// 原生适配器保留 BytesSource；令牌不会交给原生媒体引擎。
class _NativeClipPlayer implements AudioClipPlayer {
  final AudioPlayer _player = AudioPlayer();
  @override
  Stream<Duration> get positions => _player.onPositionChanged;
  @override
  Stream<PlayerState> get states => _player.onPlayerStateChanged;
  @override
  Stream<AudioEvent> get events => _player.eventStream;
  @override
  Future<void> initialize() => _player.setReleaseMode(ReleaseMode.stop);
  @override
  Future<void> setSource(BytesSource source) => _player.setSource(source);
  @override
  Future<void> resume() => _player.resume();
  @override
  Future<void> pause() => _player.pause();
  @override
  Future<void> seek(Duration position) => _player.seek(position);
  @override
  Future<void> dispose() => _player.dispose();
}

/// 一个实例只允许一条原生命令在途。界面超时不等于原生命令已取消，
/// 因此保留原始 Future，待其结束后才释放实例，避免迟到命令污染重试。
class _PlayerLease {
  _PlayerLease(this.player);
  final AudioClipPlayer player;
  final subscriptions = <StreamSubscription<dynamic>>[];
  final failed = Completer<void>();
  Future<void> pending = Future<void>.value();
  bool initialized = false;
  bool retired = false;
  Future<void>? retirement;

  Future<T> invoke<T>(Future<T> Function() action) {
    final result = Future<T>.sync(action);
    // 这里仅记录结束时机；调用方仍会从 result 收到原始错误。
    pending = result.then<void>((_) {}, onError: (Object _, StackTrace _) {});
    return result;
  }
}

final _activeAudioPlayers = <_PlayerLease>{};

/// 只释放播放器，不涉及录音、模型或工程。相同实例始终复用同一份释放 Future，
/// 避免页面卸载与应用退出同时重复 dispose 原生对象。
Future<void> _releaseAudioPlayer(_PlayerLease lease) {
  return lease.retirement ??= () async {
    lease.retired = true;
    if (!lease.failed.isCompleted) lease.failed.complete();
    await Future.wait(lease.subscriptions.map((item) => item.cancel()));
    await lease.pending;
    await lease.player.dispose();
    _activeAudioPlayers.remove(lease);
  }();
}

/// 窗口退出前给原生音频一个有界收尾机会。超时不冒充“已取消”，原始释放仍在
/// 继续；返回 false 让窗口记录固定诊断并走 Windows 正常关闭流程，不能无限转圈。
Future<bool> stopAndDisposeAllAudio({
  Duration timeout = const Duration(seconds: 3),
}) async {
  try {
    await Future.wait(_activeAudioPlayers.toList().map(_releaseAudioPlayer))
        .timeout(timeout);
    return true;
  } catch (_) {
    return false;
  }
}

class _StalePlayback implements Exception {
  const _StalePlayback();
}

class _NativePlaybackFailure implements Exception {
  const _NativePlaybackFailure();
}

/// 紧凑的本地音频播放器。只有点击播放才经受控 API 获取 WAV，绝不自动外发。
/// 认证令牌留在 API 请求头中，不拼接到媒体 URL、日志或历史记录。
class AudioClip extends StatefulWidget {
  const AudioClip({
    super.key,
    required this.api,
    required this.clip,
    this.playerFactory,
  });
  final WorkbenchApi api;
  final JsonMap clip;

  /// 仅用于隔离播放器回归测试；业务页面统一使用默认原生适配器。
  @visibleForTesting
  final AudioClipPlayer Function()? playerFactory;

  @override
  State<AudioClip> createState() => _AudioClipState();
}

class _AudioClipState extends State<AudioClip> {
  _PlayerLease? _player;
  Future<void>? _retirement;
  bool _cleanupFailed = false;
  Duration _position = Duration.zero;
  Duration _duration = Duration.zero;
  PlayerState _state = PlayerState.stopped;
  bool _loading = false;
  bool _loaded = false;
  String? _error;
  String? _stage;
  int _sourceRevision = 0;
  int _attemptRevision = 0;

  /// 晚到事件只属于原实例。失效实例既不能刷新界面，也不能触发新的播放。
  bool _owns(_PlayerLease lease) =>
      mounted && identical(_player, lease) && !lease.retired;

  _PlayerLease _createPlayer() {
    final lease = _PlayerLease(
      widget.playerFactory?.call() ?? _NativeClipPlayer(),
    );
    _player = lease;
    _activeAudioPlayers.add(lease);
    void nativeError(Object _, StackTrace _) {
      if (!_owns(lease)) return;
      if (!lease.failed.isCompleted) lease.failed.complete();
      // 加载中的错误交给统一操作边界；播放期间的异步错误也必须可见。
      if (!_loading) {
        setState(() => _error = '音频播放失败，请重试。');
        _retire(lease);
      }
    }

    lease.subscriptions.addAll([
      lease.player.positions.listen((value) {
        if (_owns(lease)) setState(() => _position = value);
      }, onError: nativeError),
      lease.player.states.listen((value) {
        if (_owns(lease)) setState(() => _state = value);
      }, onError: nativeError),
      lease.player.events.listen((event) {
        if (_owns(lease) &&
            event.eventType == AudioEventType.duration &&
            event.duration != null) {
          setState(() => _duration = event.duration!);
        }
      }, onError: nativeError),
    ]);
    return lease;
  }

  /// 释放不与原生命令竞跑。清理尚未完成时，重试只等待同一份清理任务，
  /// 不创建第二个播放器；清理失败则明确要求重启，不能假定旧设备已释放。
  void _retire(_PlayerLease lease) {
    if (lease.retired) return;
    lease.retired = true;
    if (!lease.failed.isCompleted) lease.failed.complete();
    if (identical(_player, lease)) _player = null;
    _loaded = false;
    _state = PlayerState.stopped;
    _retirement = () async {
      try {
        await _releaseAudioPlayer(lease);
      } catch (_) {
        // 不显示原生错误正文，其中可能包含本机文件路径。
        _cleanupFailed = true;
      }
    }();
  }

  /// 每个阶段都有总时限，包括初始化和 resume 等插件本身未设置超时的调用。
  /// timeout 仅放弃等待，原始 Future 由租用对象保留，不能把它当作取消成功。
  Future<T> _step<T>(
    String label,
    Duration timeout,
    Future<T> Function() action, {
    _PlayerLease? lease,
  }) async {
    setState(() => _stage = label);
    final work = Future<T>.sync(action);
    return Future.any<T>([
      work,
      if (lease != null)
        lease.failed.future.then<T>(
          (_) => throw const _NativePlaybackFailure(),
        ),
    ]).timeout(timeout, onTimeout: () => throw TimeoutException(label));
  }

  void _checkCurrent(int source, int attempt, _PlayerLease lease) {
    if (!mounted ||
        source != _sourceRevision ||
        attempt != _attemptRevision ||
        !_owns(lease)) {
      throw const _StalePlayback();
    }
  }

  /// 所有按钮和拖动命令共用串行入口，失败后撤销当前租用，下一次重新初始化。
  Future<void> _perform(
    Future<void> Function(_PlayerLease lease, int source, int attempt) action,
  ) async {
    if (_loading) return;
    final source = _sourceRevision;
    final attempt = ++_attemptRevision;
    _PlayerLease? lease;
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final retirement = _retirement;
      if (retirement != null) {
        await _step('清理上次播放', const Duration(seconds: 3), () => retirement);
        if (!mounted ||
            source != _sourceRevision ||
            attempt != _attemptRevision) {
          throw const _StalePlayback();
        }
        _retirement = null;
      }
      if (_cleanupFailed) {
        throw const ApiException('播放器未能安全释放，请重新打开应用后重试。');
      }
      lease = _player ?? _createPlayer();
      if (!lease.initialized) {
        final initializing = lease;
        await _step(
          '初始化播放器',
          const Duration(seconds: 10),
          () => initializing.invoke(initializing.player.initialize),
          lease: lease,
        );
        _checkCurrent(source, attempt, lease);
        lease.initialized = true;
      }
      await action(lease, source, attempt);
    } catch (error) {
      if (lease != null) _retire(lease);
      if (mounted && source == _sourceRevision && attempt == _attemptRevision) {
        setState(() {
          _error = error is TimeoutException
              ? error.message == '清理上次播放'
                    ? '上次播放仍在清理，请稍后重试。'
                    : '${error.message}超时，请重试。'
              : error is ApiException
              ? error.message
              : '音频播放失败，请重试。';
        });
      }
    } finally {
      if (mounted && attempt == _attemptRevision) {
        setState(() {
          _loading = false;
          _stage = null;
        });
      }
    }
  }

  @override
  void didUpdateWidget(covariant AudioClip oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.clip['id'] != widget.clip['id'] ||
        oldWidget.clip['url'] != widget.clip['url'] ||
        oldWidget.clip['available'] != widget.clip['available']) {
      // 列表重用组件时，使尚未完成的旧音频读取失效，防止播放错段。
      _sourceRevision++;
      _attemptRevision++;
      if (_player != null) _retire(_player!);
      _loaded = false;
      _loading = false;
      _stage = null;
      _position = Duration.zero;
      _duration = Duration.zero;
      _error = null;
    }
  }

  Future<void> _toggle() => _perform((lease, source, attempt) async {
    if (_state == PlayerState.playing) {
      await _step(
        '暂停播放',
        const Duration(seconds: 8),
        () => lease.invoke(lease.player.pause),
        lease: lease,
      );
      return;
    }
    if (!_loaded) {
      final path = widget.clip['url']?.toString() ?? '';
      final data = await _step(
        '读取音频',
        const Duration(seconds: 30),
        () => widget.api.readAudio(path),
        lease: lease,
      );
      _checkCurrent(source, attempt, lease);
      await _step(
        '准备播放',
        const Duration(seconds: 20),
        () => lease.invoke(
          () =>
              lease.player.setSource(BytesSource(data, mimeType: 'audio/wav')),
        ),
        lease: lease,
      );
      _checkCurrent(source, attempt, lease);
      _loaded = true;
    }
    if (_state == PlayerState.completed) {
      await _step(
        '准备播放',
        const Duration(seconds: 8),
        () => lease.invoke(() => lease.player.seek(Duration.zero)),
        lease: lease,
      );
      _checkCurrent(source, attempt, lease);
    }
    await _step(
      '开始播放',
      const Duration(seconds: 8),
      () => lease.invoke(lease.player.resume),
      lease: lease,
    );
  });

  Future<void> _seek(double value) => _perform((lease, source, attempt) async {
    await _step(
      '调整播放位置',
      const Duration(seconds: 8),
      () => lease.invoke(
        () => lease.player.seek(Duration(milliseconds: value.round())),
      ),
      lease: lease,
    );
  });

  String _time(Duration duration) =>
      '${duration.inMinutes}:${(duration.inSeconds % 60).toString().padLeft(2, '0')}';

  @override
  Widget build(BuildContext context) {
    final available =
        widget.clip['available'] != false && widget.clip['deleted'] != true;
    final knownSeconds = widget.clip['durationSeconds'];
    // 加载前显示素材索引给出的真实时长；加载后以播放器解码时长为准。
    final duration = _duration > Duration.zero
        ? _duration
        : knownSeconds is num && knownSeconds.isFinite && knownSeconds > 0
        ? Duration(milliseconds: (knownSeconds * 1000).round())
        : Duration.zero;
    final limit = duration.inMilliseconds.toDouble();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            IconButton(
              tooltip: _loading
                  ? _stage ?? '正在加载音频'
                  : available
                  ? (_state == PlayerState.playing ? '暂停试听' : '播放本地音频')
                  : '音频已不可用',
              onPressed: available && !_loading ? _toggle : null,
              icon: _loading
                  ? const SizedBox.square(
                      dimension: 16,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : Icon(
                      _state == PlayerState.playing
                          ? Icons.pause_rounded
                          : Icons.play_arrow_rounded,
                      size: 20,
                    ),
            ),
            Expanded(
              child: SliderTheme(
                data: SliderTheme.of(context).copyWith(
                  trackHeight: 2,
                  thumbShape: const RoundSliderThumbShape(
                    enabledThumbRadius: 5,
                  ),
                ),
                child: Slider(
                  value: _position.inMilliseconds.toDouble().clamp(
                    0,
                    limit > 0 ? limit : 1,
                  ),
                  max: limit > 0 ? limit : 1,
                  onChanged: available && _loaded && !_loading && limit > 0
                      ? (value) => setState(
                          () =>
                              _position = Duration(milliseconds: value.round()),
                        )
                      : null,
                  // 拖动期间仅移动游标，松手后发送一次 seek，避免原生命令堆积。
                  onChangeEnd: available && _loaded && !_loading && limit > 0
                      ? _seek
                      : null,
                ),
              ),
            ),
            Text(
              '${_time(_position)} / ${_time(duration)}',
              style: Theme.of(context).textTheme.labelSmall,
            ),
          ],
        ),
        if (_stage != null || _error != null)
          Padding(
            padding: const EdgeInsets.only(left: 12, bottom: 6),
            child: Semantics(
              liveRegion: true,
              child: Text(
                _stage ?? _error!,
                style: Theme.of(context).textTheme.bodySmall,
              ),
            ),
          ),
      ],
    );
  }

  @override
  void dispose() {
    _attemptRevision++;
    if (_player != null) _retire(_player!);
    super.dispose();
  }
}
