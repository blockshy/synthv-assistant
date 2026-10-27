import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../core/api.dart';
import '../widgets/audio_clip.dart';

/// 录音页独立读取当前选区；真实采集、工程播放及听评仍交给后端任务。
/// 任务只提交一次，页面计时表示等待时长，不模拟录制或模型生成的百分比。
class RecordingPage extends StatefulWidget {
  const RecordingPage({super.key, required this.api, required this.onAttach});
  final WorkbenchApi api;
  final ValueChanged<List<JsonMap>> onAttach;
  @override
  State<RecordingPage> createState() => _RecordingPageState();
}

class _RecordingPageState extends State<RecordingPage> {
  final _form = GlobalKey<FormState>();
  final _name = TextEditingController(text: '调教片段');
  final _start = TextEditingController(text: '0');
  final _duration = TextEditingController(text: '10');
  final _prompt = TextEditingController(
    text: '请围绕自然度、音准稳定性与尾音分析所选歌声。若有两段，先判断是否可比，再比较对应位置；A/B 仅表示顺序，不预设 B 更好。给出最多三项可试听验证的局部建议。',
  );
  List<JsonMap> _recordings = [];
  JsonMap? _lastRecording;
  JsonMap? _comparison;
  JsonMap _status = {};
  String? _a;
  String? _b;
  String? _feedback;
  String? _review;
  String? _phase;
  bool _loading = true;
  bool _reading = false;
  int _elapsed = 0;
  Timer? _timer;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final values = await Future.wait([
        widget.api.getJson('/api/recordings'),
        widget.api.getJson('/api/status'),
      ]);
      final recordings = (values[0]['items'] as List)
          .map(
            (item) => {
              ...Map<String, dynamic>.from(item as Map),
              'kind': 'recording',
            },
          )
          .toList();
      if (mounted) {
        setState(() {
          _recordings = recordings;
          _status = Map<String, dynamic>.from(values[1] as Map);
          if (!_recordings.any((item) => item['id'] == _a)) _a = null;
          if (!_recordings.any((item) => item['id'] == _b)) _b = null;
        });
      }
    } catch (error) {
      if (mounted) setState(() => _feedback = error.toString());
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _readSelection() async {
    // 先清空旧范围。选区读取失败时，表单校验会阻止误录上一段音频。
    setState(() {
      _reading = true;
      _start.clear();
      _duration.clear();
      _feedback = null;
    });
    try {
      final result = await widget.api.exclusive(
        () => widget.api.getJson('/api/selection'),
      );
      final start = (result['startSeconds'] as num?)?.toDouble();
      final end = (result['endSeconds'] as num?)?.toDouble();
      if (start == null ||
          end == null ||
          !start.isFinite ||
          !end.isFinite ||
          start < 0 ||
          end <= start ||
          (result['noteCount'] as num? ?? 0) <= 0) {
        throw const ApiException('未读取到有效音符选区，请在 SynthV 中选择音符后重试。');
      }
      if (!mounted) return;
      setState(() {
        _start.text = start.toStringAsFixed(3);
        _duration.text = (end - start).clamp(1, 30).toStringAsFixed(3);
        _feedback = end - start > 30
            ? '已读取选区；选区超过 30 秒，本次只录制前 30 秒。'
            : end - start < 1
            ? '已读取选区；录制时长设为最少 1 秒。'
            : '已填入当前选区范围。';
      });
    } catch (error) {
      if (mounted) setState(() => _feedback = error.toString());
    } finally {
      if (mounted) setState(() => _reading = false);
    }
  }

  Future<dynamic> _runJob(String path, JsonMap payload, String phase) async {
    if (mounted) {
      setState(() {
        _phase = phase;
        _elapsed = 0;
        _feedback = null;
      });
    }
    final watch = Stopwatch()..start();
    _timer = Timer.periodic(const Duration(seconds: 1), (_) {
      if (mounted) setState(() => _elapsed = watch.elapsed.inSeconds);
    });
    try {
      return await widget.api.exclusive(() async {
        final task = await widget.api.postJson(path, payload);
        return widget.api.waitForJob(task['jobId'].toString());
      });
    } finally {
      _timer?.cancel();
      _timer = null;
      if (mounted) setState(() => _phase = null);
    }
  }

  Future<void> _record() async {
    if (!_form.currentState!.validate()) return;
    try {
      final result = await _runJob('/api/record', {
        'label': _name.text.trim(),
        'startSeconds': double.parse(_start.text),
        'durationSeconds': double.parse(_duration.text),
      }, '正在录制 SynthV 工程输出');
      if (!mounted) return;
      setState(() {
        _lastRecording = {
          ...Map<String, dynamic>.from(result as Map),
          'kind': 'recording',
        };
        _feedback = '录制完成并保存到素材库。尚未发送给模型。';
      });
      await _load();
    } catch (error) {
      if (mounted) setState(() => _feedback = error.toString());
    }
  }

  Future<void> _compare() async {
    if (_a == null || _b == null || _a == _b) {
      setState(() => _feedback = '请选择两段不同的录音。');
      return;
    }
    try {
      final result = await widget.api.exclusive(
        () => widget.api.postJson('/api/compare', {
          'beforeId': _a,
          'afterId': _b,
        }),
      );
      if (mounted) {
        setState(() {
          _comparison = Map<String, dynamic>.from(result as Map);
          _feedback = '已完成本地电平分析。';
        });
      }
    } catch (error) {
      if (mounted) setState(() => _feedback = error.toString());
    }
  }

  Future<void> _reviewAudio() async {
    if (_a == null || _prompt.text.trim().isEmpty) {
      setState(() => _feedback = '请选择音频 A 并填写听评目标。');
      return;
    }
    try {
      final result = await _runJob('/api/review', {
        'recordingIds': [_a, if (_b != null && _b != _a) _b],
        'prompt': _prompt.text.trim(),
      }, '正在等待音频听评');
      if (!mounted) return;
      if (result['status'] != 'ok') {
        throw ApiException(result['message']?.toString() ?? '听评未完成。');
      }
      setState(() {
        _review = result['review']?.toString() ?? '';
        _feedback = '听评完成；建议尚未应用到工程。';
      });
    } catch (error) {
      if (mounted) setState(() => _feedback = error.toString());
    }
  }

  String _number(dynamic value, [int digits = 2]) =>
      value is num && value.isFinite ? value.toStringAsFixed(digits) : '—';
  JsonMap _attachment(JsonMap item) => {
    for (final key in [
      'id',
      'kind',
      'name',
      'label',
      'url',
      'durationSeconds',
      'available',
    ])
      if (item.containsKey(key)) key: item[key],
  };

  Widget _section(String title, List<Widget> children) => Card(
    margin: const EdgeInsets.only(bottom: 16),
    child: Padding(
      padding: const EdgeInsets.all(20),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text(title, style: Theme.of(context).textTheme.titleMedium),
          const SizedBox(height: 16),
          ...children,
        ],
      ),
    ),
  );

  Widget _timeField(
    String label,
    TextEditingController controller,
    double min,
    double max,
  ) => TextFormField(
    controller: controller,
    enabled: !widget.api.busy && !_reading,
    keyboardType: const TextInputType.numberWithOptions(decimal: true),
    decoration: InputDecoration(labelText: label, suffixText: '秒'),
    validator: (text) {
      final value = double.tryParse(text ?? '');
      return value == null || !value.isFinite || value < min || value > max
          ? '填写 $min～$max 秒'
          : null;
    },
  );

  Widget _recordingPicker(
    String label,
    String? value,
    ValueChanged<String?> onChanged,
  ) => DropdownButtonFormField<String>(
    key: ValueKey('$label:$value:${_recordings.length}'),
    initialValue: value ?? '',
    isExpanded: true,
    decoration: InputDecoration(labelText: label),
    items: [
      const DropdownMenuItem(value: '', child: Text('不选择')),
      ..._recordings.map(
        (item) => DropdownMenuItem(
          value: item['id'].toString(),
          child: Text(
            item['label']?.toString() ?? '录音',
            overflow: TextOverflow.ellipsis,
          ),
        ),
      ),
    ],
    onChanged: widget.api.busy
        ? null
        : (selected) => setState(() {
            onChanged(selected == '' ? null : selected);
            _comparison = null;
            _review = null;
          }),
  );

  Widget _metrics(JsonMap analysis) => Wrap(
    spacing: 24,
    runSpacing: 12,
    children: [
      for (final entry in {
        '均方根电平': '${_number(analysis['rmsDbfs'])} dBFS',
        '峰值': '${_number(analysis['peakDbfs'])} dBFS',
        '采样率': '${_number(analysis['sampleRate'], 0)} Hz',
      }.entries)
        Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(entry.key, style: Theme.of(context).textTheme.bodySmall),
            const SizedBox(height: 4),
            Text(entry.value, style: Theme.of(context).textTheme.titleSmall),
          ],
        ),
    ],
  );

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: widget.api,
    builder: (context, _) {
      final busy = widget.api.busy;
      final configured = (_status['review'] as Map?)?['configured'] == true;
      return Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Padding(
            padding: const EdgeInsets.all(24),
            child: Row(
              children: [
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        '录音与试听',
                        style: Theme.of(context).textTheme.headlineSmall,
                      ),
                      const SizedBox(height: 4),
                      Text(
                        '采集 SynthV 当前工程混音，保留真实音频作为调教依据。',
                        style: Theme.of(context).textTheme.bodySmall,
                      ),
                    ],
                  ),
                ),
                IconButton(
                  tooltip: '刷新录音与服务状态',
                  onPressed: busy || _loading ? null : _load,
                  icon: const Icon(Icons.refresh_rounded),
                ),
              ],
            ),
          ),
          Expanded(
            child: SingleChildScrollView(
              padding: const EdgeInsets.fromLTRB(24, 0, 24, 16),
              child: Align(
                alignment: Alignment.topCenter,
                child: ConstrainedBox(
                  constraints: const BoxConstraints(maxWidth: 880),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      _section('录制范围', [
                        Form(
                          key: _form,
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.stretch,
                            children: [
                              TextFormField(
                                controller: _name,
                                enabled: !busy,
                                maxLength: 100,
                                decoration: const InputDecoration(
                                  labelText: '录音名称',
                                  counterText: '',
                                ),
                                validator: (text) => (text ?? '').trim().isEmpty
                                    ? '请填写录音名称'
                                    : null,
                              ),
                              const SizedBox(height: 16),
                              LayoutBuilder(
                                builder: (context, constraints) {
                                  final fields = [
                                    _timeField('起点', _start, 0, 86400),
                                    _timeField('时长', _duration, 1, 30),
                                  ];
                                  return constraints.maxWidth < 380
                                      ? Column(
                                          children: [
                                            fields[0],
                                            const SizedBox(height: 12),
                                            fields[1],
                                          ],
                                        )
                                      : Row(
                                          children: [
                                            Expanded(child: fields[0]),
                                            const SizedBox(width: 16),
                                            Expanded(child: fields[1]),
                                          ],
                                        );
                                },
                              ),
                              const SizedBox(height: 12),
                              Align(
                                alignment: Alignment.centerLeft,
                                child: OutlinedButton.icon(
                                  onPressed: busy || _reading
                                      ? null
                                      : _readSelection,
                                  icon: _reading
                                      ? const SizedBox.square(
                                          dimension: 16,
                                          child: CircularProgressIndicator(
                                            strokeWidth: 2,
                                          ),
                                        )
                                      : const Icon(
                                          Icons.select_all_rounded,
                                          size: 18,
                                        ),
                                  label: const Text('读取选区'),
                                ),
                              ),
                              const SizedBox(height: 8),
                              Text(
                                '每次录制 1～30 秒。读取选区只填入时间，不开始录音。',
                                style: Theme.of(context).textTheme.bodySmall,
                              ),
                            ],
                          ),
                        ),
                      ]),
                      if (_lastRecording != null)
                        _section('最近录制', [
                          Text(_lastRecording!['label']?.toString() ?? '录音'),
                          AudioClip(
                            key: ValueKey(_lastRecording!['id']),
                            api: widget.api,
                            clip: _lastRecording!,
                          ),
                          if (_lastRecording!['analysis'] is Map) ...[
                            const SizedBox(height: 8),
                            SizedBox(
                              height: 64,
                              child: CustomPaint(
                                painter: _WaveformPainter(
                                  (_lastRecording!['analysis']['envelope'] ??
                                          [])
                                      as List,
                                  Theme.of(context).colorScheme.onSurface,
                                ),
                              ),
                            ),
                            const SizedBox(height: 16),
                            _metrics(
                              Map<String, dynamic>.from(
                                _lastRecording!['analysis'] as Map,
                              ),
                            ),
                            if ((_lastRecording!['analysis']['warnings']
                                        as List? ??
                                    [])
                                .isNotEmpty)
                              Padding(
                                padding: const EdgeInsets.only(top: 12),
                                child: Text(
                                  (_lastRecording!['analysis']['warnings']
                                          as List)
                                      .join('\n'),
                                  style: Theme.of(context).textTheme.bodySmall,
                                ),
                              ),
                          ],
                          const SizedBox(height: 12),
                          Align(
                            alignment: Alignment.centerLeft,
                            child: FilledButton.icon(
                              onPressed: busy
                                  ? null
                                  : () => widget.onAttach([
                                      _attachment(_lastRecording!),
                                    ]),
                              icon: const Icon(
                                Icons.attach_file_rounded,
                                size: 18,
                              ),
                              label: const Text('附到会话'),
                            ),
                          ),
                        ]),
                      _section('A / B 对比', [
                        LayoutBuilder(
                          builder: (context, constraints) {
                            final fields = [
                              _recordingPicker(
                                '音频 A',
                                _a,
                                (value) => _a = value,
                              ),
                              _recordingPicker(
                                '音频 B（可选）',
                                _b,
                                (value) => _b = value,
                              ),
                            ];
                            return constraints.maxWidth < 440
                                ? Column(
                                    children: [
                                      fields[0],
                                      const SizedBox(height: 12),
                                      fields[1],
                                    ],
                                  )
                                : Row(
                                    children: [
                                      Expanded(child: fields[0]),
                                      const SizedBox(width: 16),
                                      Expanded(child: fields[1]),
                                    ],
                                  );
                          },
                        ),
                        const SizedBox(height: 12),
                        for (final id in [
                          _a,
                          _b,
                        ].whereType<String>().toSet()) ...[
                          Text(
                            id == _a ? 'A' : 'B',
                            style: Theme.of(context).textTheme.labelSmall,
                          ),
                          AudioClip(
                            key: ValueKey('comparison:$id'),
                            api: widget.api,
                            clip: _recordings.firstWhere(
                              (item) => item['id'] == id,
                            ),
                          ),
                        ],
                        const SizedBox(height: 8),
                        Wrap(
                          spacing: 8,
                          runSpacing: 8,
                          children: [
                            OutlinedButton.icon(
                              onPressed:
                                  busy || _a == null || _b == null || _a == _b
                                  ? null
                                  : _compare,
                              icon: const Icon(
                                Icons.equalizer_rounded,
                                size: 18,
                              ),
                              label: const Text('本地电平分析'),
                            ),
                            OutlinedButton.icon(
                              onPressed: busy || _a == null
                                  ? null
                                  : () => widget.onAttach(
                                      _recordings
                                          .where(
                                            (item) =>
                                                item['id'] == _a ||
                                                item['id'] == _b,
                                          )
                                          .map(_attachment)
                                          .toList()
                                        ..sort(
                                          (a, b) => a['id'] == _a ? -1 : 1,
                                        ),
                                    ),
                              icon: const Icon(
                                Icons.attach_file_rounded,
                                size: 18,
                              ),
                              label: const Text('附到会话'),
                            ),
                          ],
                        ),
                        if (_comparison != null) ...[
                          const SizedBox(height: 16),
                          Text(
                            'B − A：峰值 ${_number(_comparison!['delta']?['peakDb'])} dB · 均方根 ${_number(_comparison!['delta']?['rmsDb'])} dB',
                          ),
                          const SizedBox(height: 8),
                          Text(
                            _comparison!['interpretation']?.toString() ?? '',
                            style: Theme.of(context).textTheme.bodySmall,
                          ),
                          if ((_comparison!['warnings'] as List? ?? [])
                              .isNotEmpty)
                            Padding(
                              padding: const EdgeInsets.only(top: 8),
                              child: Text(
                                (_comparison!['warnings'] as List).join('\n'),
                                style: Theme.of(context).textTheme.bodySmall,
                              ),
                            ),
                        ],
                        const SizedBox(height: 20),
                        TextField(
                          controller: _prompt,
                          enabled: !busy,
                          minLines: 3,
                          maxLines: 6,
                          maxLength: 4000,
                          decoration: const InputDecoration(
                            labelText: '听评目标',
                            counterText: '',
                          ),
                        ),
                        const SizedBox(height: 12),
                        Text(
                          configured
                              ? '提交听评会将所选音频发送到“默认配置”中的模型，可能产生 API 费用。'
                              : '请先在模型设置的默认配置中配置支持音频输入的模型。',
                          style: Theme.of(context).textTheme.bodySmall,
                        ),
                        const SizedBox(height: 12),
                        Align(
                          alignment: Alignment.centerLeft,
                          child: FilledButton.icon(
                            onPressed: busy || _a == null || !configured
                                ? null
                                : _reviewAudio,
                            icon: const Icon(
                              Icons.graphic_eq_rounded,
                              size: 18,
                            ),
                            label: const Text('提交音频听评'),
                          ),
                        ),
                        if (_review != null)
                          Padding(
                            padding: const EdgeInsets.only(top: 20),
                            child: SelectableText(_review!),
                          ),
                      ]),
                    ],
                  ),
                ),
              ),
            ),
          ),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 16),
            decoration: BoxDecoration(
              border: Border(
                top: BorderSide(color: Theme.of(context).dividerColor),
              ),
            ),
            child: Row(
              children: [
                if (_phase != null) ...[
                  const SizedBox.square(
                    dimension: 16,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  ),
                  const SizedBox(width: 12),
                ],
                Expanded(
                  child: Text(
                    _phase != null
                        ? '$_phase · 已等待 $_elapsed 秒'
                        : _feedback ?? '录音保存于本地，随时可附加到会话。',
                    style: Theme.of(context).textTheme.bodySmall,
                  ),
                ),
                const SizedBox(width: 12),
                FilledButton.icon(
                  onPressed: busy || _reading ? null : _record,
                  icon: const Icon(Icons.fiber_manual_record_rounded, size: 14),
                  label: const Text('开始录制'),
                ),
              ],
            ),
          ),
        ],
      );
    },
  );

  @override
  void dispose() {
    _timer?.cancel();
    for (final controller in [_name, _start, _duration, _prompt]) {
      controller.dispose();
    }
    super.dispose();
  }
}

/// 波形只呈现后端真实包络采样，不从文件名或时长制造装饰性的假波形。
class _WaveformPainter extends CustomPainter {
  _WaveformPainter(this.samples, this.color);
  final List samples;
  final Color color;
  @override
  void paint(Canvas canvas, Size size) {
    if (samples.isEmpty) return;
    final paint = Paint()
      ..color = color.withValues(alpha: 0.55)
      ..strokeWidth = math.max(1, size.width / samples.length * 0.65);
    for (var index = 0; index < samples.length; index++) {
      final value = samples[index] is Map
          ? (samples[index]['peak'] as num?)?.toDouble() ?? 0
          : 0.0;
      final height = value.isFinite
          ? value.clamp(0, 1) * size.height * 0.48
          : 0.0;
      final x = (index + 0.5) * size.width / samples.length;
      canvas.drawLine(
        Offset(x, size.height / 2 - height),
        Offset(x, size.height / 2 + height),
        paint,
      );
    }
  }

  @override
  bool shouldRepaint(covariant _WaveformPainter oldDelegate) =>
      oldDelegate.samples != samples || oldDelegate.color != color;
}
