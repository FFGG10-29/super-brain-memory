import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';

import 'brain_client.dart';
import 'pages.dart' show PageFrame;

/// 顶栏实时仪表：Qt 桌面端「CPU / 内存 / 显卡」三表的 Flutter 等价实现。
/// 数据来自后端 `system.stats` RPC（psutil + nvidia-smi，2 秒采样一次），
/// 前端只读缓存、不跑子进程；没有 N 卡时显卡位显示「—」而不是假装 0%。
class SystemGauges extends StatefulWidget {
  const SystemGauges({super.key, required this.client, this.compact = true});
  final BrainClient client;
  final bool compact;

  @override
  State<SystemGauges> createState() => _SystemGaugesState();
}

class _SystemGaugesState extends State<SystemGauges> {
  Timer? _timer;
  Map<String, dynamic> _stats = <String, dynamic>{};

  @override
  void initState() {
    super.initState();
    unawaited(_poll());
    _timer = Timer.periodic(const Duration(seconds: 2), (_) => unawaited(_poll()));
  }

  Future<void> _poll() async {
    final data = await widget.client.systemStats();
    if (!mounted) return;
    setState(() => _stats = data);
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final cpu = _asPercent(_stats['cpu_percent']);
    final memory = _asPercent(_stats['memory_percent']);
    final gpu = _stats['gpu'] is Map<String, dynamic>
        ? _stats['gpu'] as Map<String, dynamic>
        : <String, dynamic>{};
    final gpuPercent = _asPercent(gpu['util_percent']);
    final size = widget.compact ? 30.0 : 108.0;
    final gauges = <Widget>[
      _gauge('CPU', cpu, const Color(0xFF4FC3F7), size,
          'CPU ${_percentText(cpu)} · ${_stats['sampling'] == true ? '实时采样' : '等待首帧'}'),
      _gauge('内存', memory, const Color(0xFF7FE9FF), size,
          '内存 ${_percentText(memory)} · ${_gb(_stats['memory_used_gb'])} / ${_gb(_stats['memory_total_gb'])} GB'),
      _gauge('显卡', gpuPercent, const Color(0xFFE5BC85), size,
          gpu.isEmpty
              ? '显卡：未检测到 NVIDIA GPU 或 nvidia-smi 不可用'
              : '${gpu['name']} · ${_percentText(gpuPercent)} · '
                '${_gb(_num(gpu['memory_used_mb']) / 1024)}/${_gb(_num(gpu['memory_total_mb']) / 1024)} GB · '
                '${_num(gpu['temperature_c']).toStringAsFixed(0)}°C'),
    ];
    if (widget.compact) {
      return Tooltip(
        message: 'CPU / 内存 / 显卡实时占用（后端 2s 采样）\n'
            '${gauges.isEmpty ? '' : _tooltip(_stats, gpu)}',
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: <Widget>[
            for (final gauge in gauges) ...<Widget>[gauge, const SizedBox(width: 8)],
          ],
        ),
      );
    }
    return Wrap(spacing: 28, runSpacing: 20, children: gauges);
  }

  Widget _gauge(String label, double? value, Color color, double size, String tip) => Tooltip(
        message: tip,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: <Widget>[
            SizedBox(
              width: size,
              height: size,
              child: CustomPaint(painter: _RingPainter(value: value, color: color, label: label)),
            ),
            if (size > 50) ...<Widget>[
              const SizedBox(height: 8),
              Text(label, style: const TextStyle(color: Color(0xFF8FA6C4), fontSize: 12)),
            ],
          ],
        ),
      );

  static String _tooltip(Map<String, dynamic> stats, Map<String, dynamic> gpu) =>
      'CPU ${_percentText(_asPercent(stats['cpu_percent']))} · '
      '内存 ${_percentText(_asPercent(stats['memory_percent']))} · '
      '显卡 ${gpu.isEmpty ? '—' : _percentText(_asPercent(gpu['util_percent']))}';

  static double? _asPercent(dynamic value) {
    if (value is num) return value.toDouble().clamp(0.0, 100.0);
    return null;
  }

  static double _num(dynamic value) => value is num ? value.toDouble() : 0.0;
  static String _percentText(double? value) => value == null ? '—' : '${value.toStringAsFixed(0)}%';
  static String _gb(dynamic value) => value is num ? value.toStringAsFixed(1) : '—';
}

class _RingPainter extends CustomPainter {
  const _RingPainter({required this.value, required this.color, required this.label});
  final double? value;
  final Color color;
  final String label;

  @override
  void paint(Canvas canvas, Size size) {
    final center = Offset(size.width / 2, size.height / 2);
    final radius = math.min(size.width, size.height) / 2 - 3;
    final track = Paint()
      ..color = const Color(0xFF1B2B42)
      ..style = PaintingStyle.stroke
      ..strokeWidth = size.width > 50 ? 7 : 3;
    canvas.drawCircle(center, radius, track);
    final percent = value;
    if (percent != null) {
      final hot = percent >= 85;
      final arc = Paint()
        ..color = hot ? const Color(0xFFFF6B6B) : color
        ..style = PaintingStyle.stroke
        ..strokeWidth = size.width > 50 ? 7 : 3
        ..strokeCap = StrokeCap.round;
      canvas.drawArc(
        Rect.fromCircle(center: center, radius: radius),
        -math.pi / 2,
        2 * math.pi * (percent / 100.0),
        false,
        arc,
      );
    }
    final big = size.width > 50;
    final text = percent == null ? '—' : percent.toStringAsFixed(0);
    final painter = TextPainter(
      text: TextSpan(
        text: text,
        style: TextStyle(
          color: percent == null ? const Color(0xFF5D7690) : Colors.white,
          fontSize: big ? 24 : 10,
          fontWeight: big ? FontWeight.w600 : FontWeight.normal,
        ),
      ),
      textDirection: TextDirection.ltr,
    )..layout();
    painter.paint(canvas, center - Offset(painter.width / 2, painter.height / 2));
  }

  @override
  bool shouldRepaint(_RingPainter oldDelegate) =>
      oldDelegate.value != value || oldDelegate.color != color;
}

/// 完整硬件仪表页（顶栏是 compact 版，这里给大表 + 明细，对齐 Qt 的实时仪表区）。
class HardwarePage extends StatefulWidget {
  const HardwarePage({super.key, required this.client});
  final BrainClient client;

  @override
  State<HardwarePage> createState() => _HardwarePageState();
}

class _HardwarePageState extends State<HardwarePage> {
  Map<String, dynamic> _stats = <String, dynamic>{};
  String _status = '等待采样…';

  @override
  void initState() {
    super.initState();
    unawaited(_refresh());
  }

  Future<void> _refresh() async {
    final data = await widget.client.systemStats();
    if (!mounted) return;
    setState(() {
      _stats = data;
      _status = data['error']?.toString() ??
          '采样时间 ${DateTime.fromMillisecondsSinceEpoch((data['ts_ms'] as num? ?? 0).toInt()).toLocal().toString().substring(11, 19)} · 每 2 秒自动刷新';
    });
  }

  @override
  Widget build(BuildContext context) {
    final gpu = _stats['gpu'] is Map<String, dynamic>
        ? _stats['gpu'] as Map<String, dynamic>
        : <String, dynamic>{};
    final cpu = _stats['cpu_percent'];
    final memory = _stats['memory_percent'];
    return PageFrame(
      title: '硬件仪表',
      subtitle: '后端 system.stats：psutil（CPU/内存）+ nvidia-smi（显卡利用率/显存/温度），采样在守护线程，前端零阻塞',
      child: ListView(
        children: <Widget>[
          _card(<Widget>[
            const Text('实时占用', style: TextStyle(color: Colors.white, fontSize: 14)),
            const SizedBox(height: 18),
            Center(child: SystemGauges(client: widget.client, compact: false)),
            const SizedBox(height: 12),
            Text(_status, style: const TextStyle(color: Color(0xFF6B839F), fontSize: 11)),
          ]),
          const SizedBox(height: 12),
          _card(<Widget>[
            const Text('明细', style: TextStyle(color: Colors.white, fontSize: 14)),
            const SizedBox(height: 12),
            _row('CPU 占用', cpu == null ? '—' : '${_d(cpu).toStringAsFixed(1)} %'),
            _row('物理内存占用', memory == null ? '—' : '${_d(memory).toStringAsFixed(1)} %'),
            _row('已用 / 总内存',
                '${_f(_stats['memory_used_gb'])} / ${_f(_stats['memory_total_gb'])} GB'),
            const Divider(color: Color(0xFF142238)),
            _row('显卡', gpu.isEmpty ? '未检测到 NVIDIA GPU' : '${gpu['name']}'),
            _row('显卡利用率', gpu.isEmpty ? '—' : '${_d(gpu['util_percent']).toStringAsFixed(0)} %'),
            _row('显存',
                gpu.isEmpty ? '—' : '${(_d(gpu['memory_used_mb']) / 1024).toStringAsFixed(2)} / '
                    '${(_d(gpu['memory_total_mb']) / 1024).toStringAsFixed(2)} GB'),
            _row('显卡温度', gpu.isEmpty ? '—' : '${_d(gpu['temperature_c']).toStringAsFixed(0)} °C'),
            const SizedBox(height: 12),
            Align(
              alignment: Alignment.centerLeft,
              child: OutlinedButton(onPressed: _refresh, child: const Text('立即刷新')),
            ),
          ]),
        ],
      ),
    );
  }

  Widget _card(List<Widget> children) => Container(
        padding: const EdgeInsets.all(18),
        decoration: BoxDecoration(
          color: const Color(0xFF0A1424),
          borderRadius: BorderRadius.circular(10),
          border: Border.all(color: const Color(0xFF142238)),
        ),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: children),
      );

  Widget _row(String label, String value) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 5),
        child: Row(
          children: <Widget>[
            SizedBox(width: 130, child: Text(label, style: const TextStyle(color: Color(0xFF8FA6C4), fontSize: 12))),
            Expanded(child: Text(value, style: const TextStyle(color: Colors.white, fontSize: 12))),
          ],
        ),
      );

  static double _d(dynamic value) => value is num ? value.toDouble() : 0.0;
  static String _f(dynamic value) => value is num ? value.toStringAsFixed(1) : '—';
}
