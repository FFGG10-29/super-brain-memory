import 'dart:async';

import 'package:flutter/material.dart';

import 'brain_client.dart';

/// W9 / C4：叙事洞察页。
/// 展示 AI 第一人称叙事 neuron、Kùzu 图密度（扩散是否降 1 跳）、
/// 当前会话 PAD 分段门控状态，以及巩固任务的待重试队列。
class NarrativeInsightPage extends StatefulWidget {
  const NarrativeInsightPage({super.key, required this.client});

  final BrainClient client;

  @override
  State<NarrativeInsightPage> createState() => _NarrativeInsightPageState();
}

class _NarrativeInsightPageState extends State<NarrativeInsightPage> {
  static const Color _hint = Color(0xFF6B839F);
  static const Color _panel = Color(0xFF0A1424);

  List<Map<String, dynamic>> _narratives = <Map<String, dynamic>>[];
  List<Map<String, dynamic>> _pending = <Map<String, dynamic>>[];
  Map<String, dynamic> _density = <String, dynamic>{};
  Map<String, dynamic> _pad = <String, dynamic>{};
  String _message = '';
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  Future<void> _load() async {
    try {
      final narratives = await widget.client.narrativeList(limit: 50);
      final density = await widget.client.graphDensity();
      final pad = await widget.client.currentPad();
      final pending = await widget.client.narrativeRetryStatus();
      if (!mounted) return;
      setState(() {
        _narratives = (narratives['rows'] is List<dynamic>)
            ? (narratives['rows'] as List<dynamic>)
                .where((dynamic row) => row is Map)
                .map((dynamic row) => Map<String, dynamic>.from(row as Map))
                .toList()
            : <Map<String, dynamic>>[];
        _density = density;
        _pad = pad;
        _pending = pending;
      });
    } catch (error) {
      if (mounted) setState(() => _message = '加载失败：$error');
    }
  }

  Future<void> _reconstruct() async {
    setState(() => _busy = true);
    try {
      final result = await widget.client.narrativeReconstruct(incremental: true);
      if (!mounted) return;
      setState(() => _message = '重构完成：${result['reconstructed'] ?? 0} 条叙事，'
          '降级 ${result['degraded'] ?? 0} 条');
      await _load();
    } catch (error) {
      if (mounted) setState(() => _message = '重构失败：$error');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  static String _short(dynamic value) {
    final text = value?.toString() ?? '';
    return text.length > 8 ? text.substring(0, 8) : text;
  }

  Widget _metric(String label, String value) => Expanded(
        child: Container(
          margin: const EdgeInsets.only(right: 10),
          padding: const EdgeInsets.all(12),
          decoration: BoxDecoration(
            color: _panel,
            borderRadius: BorderRadius.circular(8),
            border: Border.all(color: const Color(0xFF1B2C45)),
          ),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: <Widget>[
            Text(label, style: const TextStyle(color: _hint, fontSize: 10)),
            const SizedBox(height: 6),
            Text(value, style: const TextStyle(color: Colors.white, fontSize: 15)),
          ]),
        ),
      );

  @override
  Widget build(BuildContext context) {
    final pad = _pad['pad'];
    String padText = '未建立';
    if (pad is List<dynamic> && pad.length >= 3) {
      padText = 'P=${pad[0]} A=${pad[1]} D=${pad[2]}';
    } else if (pad is Map) {
      padText = 'P=${pad['p'] ?? 0} A=${pad['a'] ?? 0} D=${pad['d'] ?? 0}';
    }
    return Padding(
      padding: const EdgeInsets.all(18),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: <Widget>[
        Row(children: <Widget>[
          const Text('叙事洞察', style: TextStyle(color: Colors.white, fontSize: 18)),
          const Spacer(),
          OutlinedButton(onPressed: _busy ? null : () { unawaited(_reconstruct()); },
              child: Text(_busy ? '重构中…' : '重构叙事', style: const TextStyle(fontSize: 11))),
          const SizedBox(width: 8),
          OutlinedButton(onPressed: _busy ? null : () { unawaited(_load()); },
              child: const Text('刷新', style: TextStyle(fontSize: 11))),
        ]),
        if (_message.isNotEmpty)
          Padding(
            padding: const EdgeInsets.only(top: 8),
            child: Text(_message, style: const TextStyle(color: _hint, fontSize: 11)),
          ),
        const SizedBox(height: 14),
        Row(children: <Widget>[
          _metric('叙事 neuron', '${_narratives.length} 条'),
          _metric('图平均度数', '${_density['avg_degree'] ?? '—'}'
              '${_density['sparse'] == true ? '（稀疏）' : ''}'),
          _metric('当前 PAD · w6', '$padText · ${_pad['weight_multiplier'] ?? 0}'),
          _metric('巩固待重试', '${_pending.length} 项'),
        ]),
        const SizedBox(height: 14),
        Expanded(
          child: Container(
            width: double.infinity,
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(
              color: _panel,
              borderRadius: BorderRadius.circular(8),
              border: Border.all(color: const Color(0xFF1B2C45)),
            ),
            child: _narratives.isEmpty
                ? const Center(
                    child: Text('还没有叙事 neuron；等巩固窗口跑完，或点「重构叙事」。',
                        style: TextStyle(color: _hint, fontSize: 12)))
                : ListView.separated(
                    itemCount: _narratives.length,
                    separatorBuilder: (_, __) => const Divider(height: 12, color: Color(0xFF1B2C45)),
                    itemBuilder: (context, index) {
                      final row = _narratives[index];
                      return ListTile(
                        contentPadding: EdgeInsets.zero,
                        title: Text('${row['content'] ?? ''}',
                            maxLines: 3, style: const TextStyle(color: Colors.white, fontSize: 12)),
                        subtitle: Text(
                            '${row['namespace'] ?? ''} · ${row['pyramid_subtype'] ?? ''} · '
                            '${_short(row['id'])}',
                            style: const TextStyle(color: _hint, fontSize: 10)),
                      );
                    },
                  ),
          ),
        ),
      ]),
    );
  }
}
