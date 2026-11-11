import 'dart:async';

import 'package:flutter/material.dart';

import 'brain_client.dart';

/// 缓存使用情况页：真实数据（后端 cache.metrics → CacheManager.snapshot_metrics）。
class CachePage extends StatefulWidget {
  const CachePage({super.key, required this.client});

  final BrainClient client;

  @override
  State<CachePage> createState() => _CachePageState();
}

class _CachePageState extends State<CachePage> {
  static const Color _bg = Color(0xFF0B1220);
  static const Color _panel = Color(0xFF121C2E);
  static const Color _border = Color(0xFF22334D);
  static const Color _hint = Color(0xFF7C93B2);
  static const Color _ok = Color(0xFF4ADE80);
  static const Color _warn = Color(0xFFFACC15);
  static const Color _dead = Color(0xFFF87171);

  Map<String, dynamic> _data = <String, dynamic>{};
  String _message = '';
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  Future<void> _load() async {
    setState(() => _busy = true);
    try {
      final data = await widget.client.cacheMetrics();
      if (!mounted) return;
      setState(() {
        _data = data;
        _message = '';
      });
    } catch (error) {
      if (mounted) setState(() => _message = '加载失败：$error');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Color _rateColor(double rate) =>
      rate >= 0.5 ? _ok : (rate >= 0.2 ? _warn : _hint);

  // 单命名空间指标卡：命中率进度条 + 计数行。
  Widget _nsCard(String name, Map ns) {
    final enabled = (ns['enabled'] ?? false) == true;
    final entries = (ns['entries'] ?? 0) as int;
    final capacity = (ns['capacity'] ?? 0) as int;
    final hitRate = ((ns['hit_rate'] ?? 0) as num).toDouble();
    final hits = (ns['hits'] ?? 0) as int;
    final misses = (ns['misses'] ?? 0) as int;
    final evictions = (ns['evictions'] ?? 0) as int;
    final invalidations = (ns['invalidations'] ?? 0) as int;
    final latency = (ns['avg_latency_us'] ?? 0) as num;
    return Container(
      margin: const EdgeInsets.fromLTRB(16, 8, 16, 8),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: _panel,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: _border),
      ),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: <Widget>[
        Row(children: <Widget>[
          Text(name,
              style: const TextStyle(
                  color: Colors.white, fontSize: 13, fontFamily: 'monospace')),
          const SizedBox(width: 8),
          _pill(enabled ? '开' : '关', enabled ? _ok : _hint),
          const Spacer(),
          Text('${ns['media']} · $entries/$capacity',
              style: const TextStyle(color: _hint, fontSize: 10)),
        ]),
        const SizedBox(height: 8),
        // 命中率进度条
        ClipRRect(
          borderRadius: BorderRadius.circular(4),
          child: LinearProgressIndicator(
            value: hitRate.clamp(0.0, 1.0),
            minHeight: 6,
            backgroundColor: _border,
            valueColor: AlwaysStoppedAnimation<Color>(_rateColor(hitRate)),
          ),
        ),
        const SizedBox(height: 8),
        Text(
          '命中率 ${(hitRate * 100).toStringAsFixed(1)}% · '
          '命中 $hits · 未命中 $misses · 淘汰 $evictions · 失效 $invalidations · '
          '平均 ${(latency / 1000).toStringAsFixed(1)}ms',
          style: const TextStyle(color: _hint, fontSize: 10),
        ),
      ]),
    );
  }

  Widget _pill(String text, Color tone) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
        decoration: BoxDecoration(
          color: tone.withValues(alpha: 0.14),
          borderRadius: BorderRadius.circular(999),
          border: Border.all(color: tone.withValues(alpha: 0.5)),
        ),
        child: Text(text, style: TextStyle(color: tone, fontSize: 10)),
      );

  @override
  Widget build(BuildContext context) {
    final available = (_data['available'] ?? false) == true;
    final namespaces = (_data['namespaces'] as Map<dynamic, dynamic>? ?? {});
    final entries = namespaces.entries
        .where((entry) => entry.value is Map)
        .map((entry) => MapEntry(entry.key.toString(),
            Map<String, dynamic>.from(entry.value as Map)))
        .toList()
      ..sort((a, b) => ((b.value['hit_rate'] ?? 0) as num)
          .compareTo((a.value['hit_rate'] ?? 0) as num));
    return Scaffold(
      backgroundColor: _bg,
      body: RefreshIndicator(
        onRefresh: _load,
        child: ListView(children: <Widget>[
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 14, 16, 4),
            child: Row(children: <Widget>[
              const Text('缓存使用情况',
                  style: TextStyle(color: Colors.white, fontSize: 18)),
              const Spacer(),
              Text('总开关 ${(_data['enabled'] ?? false) == true ? '开' : '关'}',
                  style: const TextStyle(color: _hint, fontSize: 11)),
              const SizedBox(width: 8),
              IconButton(
                tooltip: '刷新',
                onPressed: _busy ? null : _load,
                icon: const Icon(Icons.refresh, color: _hint, size: 20),
              ),
            ]),
          ),
          if (_message.isNotEmpty)
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 0, 16, 4),
              child: Text(_message,
                  style: const TextStyle(color: _dead, fontSize: 11)),
            ),
          if (!available)
            const Padding(
              padding: EdgeInsets.all(16),
              child: Text('缓存管理器未装配（独立脚本/测试场景），生产后端始终可用。',
                  style: TextStyle(color: _hint, fontSize: 12)),
            )
          else ...entries.map((entry) => _nsCard(entry.key, entry.value)),
          const SizedBox(height: 12),
        ]),
      ),
    );
  }
}
