import 'dart:async';

import 'package:flutter/material.dart';

import 'brain_client.dart';

/// 缓存使用情况页：真实数据（cache.metrics）+ 每种缓存的用途说明 + 运行时开关。
/// 开关走 cache.toggle（CacheManager.set_enabled，运行时覆盖、重启回到 config）。
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
  static const Color _accent = Color(0xFF6EE7B7);

  /// 每种缓存是干什么的（与 core/cache_manager.py / config.py 对齐）。
  static const Map<String, String> _descriptions = <String, String>{
    'recall': '召回结果缓存：相同查询直接复用检索结果，省一次图遍历',
    'embedding': '向量嵌入缓存（LMDB）：同一段文本不重复过 embedding 模型',
    'salience': '显著性打分缓存：省掉重复的显著度评估',
    'episodic': '情节缓冲快照：记录会话轮次与召回片段 id，供记忆更新后精确失效',
    'prompt_prefix': '提示词前缀缓存：人格注入等固定前缀不重复拼接',
    'path': '路径/导航缓存：图上走过的路径复用',
    'persona': '人格层参数缓存：L0 人格注入材料（容量小、永久）',
    'route': '模型路由决策缓存：同样的意图/负载直接复用上次选路',
    'doc_distill': '文档蒸馏缓存（LMDB 永久）：同一文档不重复蒸馏',
    'output': '输出缓存：整段回复按 prompt 精确复用，语义漂移风险最高（默认关）',
    'semantic': '语义输出缓存：近邻命中的问题直接复用回复（C17）',
    'tool_read': '幂等读工具缓存：MCP 只读工具的重复调用直接返回',
    'tool': '工具结果缓存（预留位）',
    'thalamus': '丘脑意图分类缓存（预留位）',
    'safety': '安全审查缓存（预留位）',
    'narrative': '叙事缓存（预留位：由 W8 投影缓存承担）',
    'conflict': '冲突检测缓存（预留位）',
  };

  Map<String, dynamic> _data = <String, dynamic>{};
  String _message = '';
  bool _busy = false;
  final Set<String> _toggling = <String>{};

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

  Future<void> _toggle(String ns, bool enabled) async {
    if (_toggling.contains(ns)) return;
    setState(() => _toggling.add(ns));
    try {
      final result = await widget.client.call('cache.toggle',
          <String, dynamic>{'ns': ns, 'enabled': enabled});
      if (!mounted) return;
      if (result is Map && result['ok'] == true) {
        final nsMap = (_data['namespaces'] as Map<dynamic, dynamic>? ?? {});
        final row = nsMap[ns];
        if (row is Map) {
          final updated = Map<String, dynamic>.from(row);
          updated['enabled'] = enabled;
          nsMap[ns] = updated;
        }
        setState(() => _message = '');
      } else {
        setState(() => _message =
            '开关失败：${result is Map ? (result['error'] ?? '未知原因') : '响应异常'}');
      }
    } catch (error) {
      if (mounted) setState(() => _message = '开关失败：$error');
    } finally {
      if (mounted) setState(() => _toggling.remove(ns));
    }
  }

  Color _rateColor(double rate) =>
      rate >= 0.5 ? _ok : (rate >= 0.2 ? _warn : _hint);

  // 单命名空间指标卡：用途说明 + 开关 + 命中率进度条 + 计数行。
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
        border: Border.all(color: enabled ? _border : _border.withValues(alpha: 0.5)),
      ),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: <Widget>[
        Row(children: <Widget>[
          Text(name,
              style: TextStyle(
                  color: enabled ? Colors.white : _hint,
                  fontSize: 13, fontFamily: 'monospace')),
          const SizedBox(width: 8),
          _pill(enabled ? '开' : '关', enabled ? _ok : _dead),
          const Spacer(),
          Text('${ns['media']} · $entries/$capacity',
              style: const TextStyle(color: _hint, fontSize: 10)),
          const SizedBox(width: 4),
          SizedBox(
            height: 26,
            child: Switch(
              value: enabled,
              activeThumbColor: _accent,
              activeTrackColor: _accent.withValues(alpha: 0.3),
              inactiveThumbColor: _hint,
              inactiveTrackColor: _border,
              materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
              onChanged: _toggling.contains(name)
                  ? null
                  : (value) => unawaited(_toggle(name, value)),
            ),
          ),
        ]),
        // 这个缓存是干什么的
        Padding(
          padding: const EdgeInsets.only(top: 2, bottom: 6),
          child: Text(_descriptions[name] ?? '自定义命名空间',
              style: TextStyle(
                  color: enabled ? _hint : _hint.withValues(alpha: 0.6),
                  fontSize: 11, height: 1.4)),
        ),
        // 命中率进度条
        ClipRRect(
          borderRadius: BorderRadius.circular(4),
          child: LinearProgressIndicator(
            value: enabled ? hitRate.clamp(0.0, 1.0) : 0.0,
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
      ..sort((a, b) {
        // 开着的排前面，其余按命中率降序
        final ea = (a.value['enabled'] ?? false) == true;
        final eb = (b.value['enabled'] ?? false) == true;
        if (ea != eb) return ea ? -1 : 1;
        return ((b.value['hit_rate'] ?? 0) as num)
            .compareTo((a.value['hit_rate'] ?? 0) as num);
      });
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
              Text('总开关 ${(_data['enabled'] ?? false) == true ? '开' : '关'}'
                  '（运行时开关不落盘，重启回到配置）',
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
