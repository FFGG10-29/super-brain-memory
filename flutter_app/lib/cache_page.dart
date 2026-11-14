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

  /// 每种缓存是干什么的。后端 cache.metrics 现已返回 consumer 字段（单一真源），
  /// 有则优先用后端文案；此表仅在后端未给 consumer 时兜底（旧版本/离线）。
  static const Map<String, String> _descriptions = <String, String>{
    'recall': '召回结果缓存：相同查询直接复用检索结果，省一次图遍历',
    'embedding': '向量嵌入缓存（LMDB）：同一段文本不重复过 embedding 模型',
    'vector_index': '向量索引段缓存：检索结果按查询复用，写后精确失效',
    'salience': '显著性打分缓存：省掉重复的显著度评估',
    'episodic': '情节缓冲快照：记录会话轮次与召回片段 id，供记忆更新后精确失效',
    'prompt_prefix': '提示词前缀缓存：人格注入等固定前缀不重复拼接',
    'path': '路径解析缓存：data_dir / models_dir 结果复用',
    'persona': '人格层参数缓存：大肥鱼人格快照（容量小，24 小时过期）',
    'route': '路由决策镜像：同意图/负载的选路留痕（权威仍为会话粘性）',
    'agent_brief': 'E6 心智模型：与该 Agent 的协作要点/主题/最近协作（TTL 300s）',
    'doc_distill': '文档蒸馏缓存（LMDB）：同一文档不重复提炼（72 小时过期，内容变更立即失效）',
    'output': '精确输出缓存：整段回复按 prompt 复用（sensitive 不读不写）',
    'semantic': '语义输出缓存：近邻命中的问题直接复用回复（C17）',
    'tool_read': '幂等读工具缓存：MCP 只读工具的重复调用直接返回',
    'tool': '工具面清单缓存：tools/list 的 schema 复用',
    'thalamus': '丘脑意图分类缓存：classify_intent 结果复用',
    'safety': '安全审查缓存：聊天敏感词判定结果复用',
    'narrative': '叙事投影缓存：W8 投影 L2，按 narrative_ids_hash 失效',
    'conflict': '冲突痕迹缓存：supersede 检测结果，写入后按 id 精确失效',
    'chat_mem_buffer': '聊天记忆缓冲（大肥鱼聊天）：攒满 8 条且静默 ≥3 分钟，或最长 30 分钟兜底，才批量提炼成叙事存进记忆',
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

  /// 容量占用条颜色分级：绿色 → 黄色（≥60%）→ 红色（≥85%，"快满变身"）。
  Color _usageColor(double usage) =>
      usage >= 0.85 ? _dead : (usage >= 0.60 ? _warn : _ok);

  // 单命名空间指标卡：用途 + 开关 + 容量占用条（主）+ 命中率细条（副）+ 计数行。
  // ★ 主条表达"用了多少容量"（entries/capacity），不再是命中率——
  //   之前把命中率画成满格进度条、右上角却标 1/8，造成"才用一点就满了"的错觉。
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
    // capacity<=0 表示不设上限（架构约定），视为 0% 占用、灰色，不会误报"满"
    final usage = (capacity > 0 && enabled)
        ? (entries / capacity).clamp(0.0, 1.0)
        : 0.0;
    final nearFull = capacity > 0 && usage >= 0.85;
    final consumer = (ns['consumer'] as String?) ??
        _descriptions[name] ??
        '自定义命名空间';
    return Container(
      margin: const EdgeInsets.fromLTRB(16, 8, 16, 8),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: _panel,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(
            color: nearFull
                ? _dead.withValues(alpha: 0.7)
                : (enabled ? _border : _border.withValues(alpha: 0.5))),
      ),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: <Widget>[
        Row(children: <Widget>[
          Text(name,
              style: TextStyle(
                  color: enabled ? Colors.white : _hint,
                  fontSize: 13, fontFamily: 'monospace')),
          const SizedBox(width: 8),
          _pill(enabled ? '开' : '关', enabled ? _ok : _dead),
          if (nearFull) ...<Widget>[
            const SizedBox(width: 6),
            _pill('快满·变身', _dead),
          ],
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
        // 这个缓存是干什么的（后端 consumer 单一真源）
        Padding(
          padding: const EdgeInsets.only(top: 2, bottom: 6),
          child: Text(consumer,
              style: TextStyle(
                  color: enabled ? _hint : _hint.withValues(alpha: 0.6),
                  fontSize: 11, height: 1.4)),
        ),
        // 主条：容量占用（占多少）
        ClipRRect(
          borderRadius: BorderRadius.circular(4),
          child: LinearProgressIndicator(
            value: usage,
            minHeight: 6,
            backgroundColor: _border,
            valueColor: AlwaysStoppedAnimation<Color>(_usageColor(usage)),
          ),
        ),
        const SizedBox(height: 3),
        Row(children: <Widget>[
          Text(
            capacity > 0
                ? '容量 ${(usage * 100).toStringAsFixed(0)}% ($entries/$capacity)'
                : '容量 不限（$entries 条）',
            style: TextStyle(color: _usageColor(usage), fontSize: 10),
          ),
          const Spacer(),
          Text('命中率 ${(hitRate * 100).toStringAsFixed(1)}%',
              style: TextStyle(color: _rateColor(hitRate), fontSize: 10)),
        ]),
        // 副条：命中率（细），命中率与容量彻底分开，互不遮挡
        ClipRRect(
          borderRadius: BorderRadius.circular(3),
          child: LinearProgressIndicator(
            value: enabled ? hitRate.clamp(0.0, 1.0) : 0.0,
            minHeight: 3,
            backgroundColor: _border.withValues(alpha: 0.5),
            valueColor: AlwaysStoppedAnimation<Color>(_rateColor(hitRate)),
          ),
        ),
        const SizedBox(height: 8),
        // 聊天记忆缓冲专属：实时进度行（当前攒了几条/下次触发条件/上次落库时间）
        if (name == 'chat_mem_buffer') ...<Widget>[
          Builder(builder: (BuildContext context) {
            final lastFlushMs = (ns['last_flush_ts'] ?? 0) as num;
            final lastMsgMs = (ns['last_message_ts'] ?? 0) as num;
            final fmt = (num ms) {
              if (ms <= 0) return '—';
              final t = DateTime.fromMillisecondsSinceEpoch(ms.toInt());
              return '${t.hour.toString().padLeft(2, '0')}:${t.minute.toString().padLeft(2, '0')}';
            };
            final bufSinceMs = (ns['buffer_since'] ?? 0) as num;
            final pendingMin = bufSinceMs > 0
                ? DateTime.now().difference(
                      DateTime.fromMillisecondsSinceEpoch(bufSinceMs.toInt())).inMinutes
                : 0;
            return Text(
              '已攒 ${entries}/${capacity} 条（已压 $pendingMin 分钟）· '
              '上次落库 ${fmt(lastFlushMs)} · 你最后说话 ${fmt(lastMsgMs)}'
              '${(ns['flushing'] ?? false) == true ? ' · 正在批量存入…' : ''}',
              style: TextStyle(
                  color: entries >= capacity ? _warn : _hint, fontSize: 10),
            );
          }),
          const SizedBox(height: 4),
        ],
        Text(
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
        // 开着的排前面；同态按"容量占用率"降序——快满的浮到顶，及时看到变身提示
        final ea = (a.value['enabled'] ?? false) == true;
        final eb = (b.value['enabled'] ?? false) == true;
        if (ea != eb) return ea ? -1 : 1;
        double usage(Map<String, dynamic> v) {
          final c = (v['capacity'] ?? 0) as int;
          final e = (v['entries'] ?? 0) as int;
          return c > 0 ? (e / c).clamp(0.0, 1.0) : 0.0;
        }
        final ua = usage(a.value);
        final ub = usage(b.value);
        if (ua != ub) return ub.compareTo(ua);
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
