import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'brain_client.dart';
import 'pages.dart' show PageFrame;

const Color _accent = Color(0xFF4FC3F7);
const Color _hint = Color(0xFF8FA6C4);
const Color _card = Color(0xFF0A1424);
const Color _border = Color(0xFF142238);

typedef OpenMemoryCallback = void Function(String id);
typedef NebulaSearchCallback = void Function(String query);

/// Qt 六路召回通道（顺序与配色来自 ui/desktop/pages/recall_page.py）。
const List<Map<String, Object>> _channels = <Map<String, Object>>[
  <String, Object>{'key': 'dense', 'label': '语义向量', 'color': Color(0xFF6EC6FF)},
  <String, Object>{'key': 'sparse', 'label': '稀疏词元', 'color': Color(0xFFB08CFF)},
  <String, Object>{'key': 'fts', 'label': '全文检索', 'color': Color(0xFF3DDC97)},
  <String, Object>{'key': 'graph', 'label': '图谱多跳', 'color': Color(0xFFFFB84D)},
  <String, Object>{'key': 'timeline', 'label': '时间线', 'color': Color(0xFFFF8FB1)},
  <String, Object>{'key': 'profile', 'label': '用户画像', 'color': Color(0xFF7FE0D4)},
];

const Map<String, Color> _typeColors = <String, Color>{
  'fact': Color(0xFF3DDC97), 'profile': Color(0xFF7FE0D4),
  'emotion': Color(0xFFFF8FB1), 'scenario': Color(0xFF6EC6FF),
  'code': Color(0xFFFFB84D),
};

Color _colorOf(String type) => _typeColors[type] ?? const Color(0xFF8FA6C4);
double _d(dynamic value) => value is num ? value.toDouble() : 0.0;
String _s(dynamic value) => value?.toString() ?? '';

Widget _box(List<Widget> children) => Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: _card,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: _border),
      ),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: children),
    );

Widget _section(String title) => Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: Text(title, style: const TextStyle(color: Colors.white, fontSize: 13, fontWeight: FontWeight.w600)),
    );

// ─────────────────────────────────────────────────────────────────────────────
// 1. 召回测试 · 检索即解释（通道分解 / 单条归因 / 注入上下文）
// ─────────────────────────────────────────────────────────────────────────────
class RecallAnalyticsPage extends StatefulWidget {
  const RecallAnalyticsPage({
    super.key,
    required this.client,
    required this.onOpenMemory,
    required this.onNebulaSearch,
  });
  final BrainClient client;
  final OpenMemoryCallback onOpenMemory;
  final NebulaSearchCallback onNebulaSearch;

  @override
  State<RecallAnalyticsPage> createState() => _RecallAnalyticsPageState();
}

class _RecallAnalyticsPageState extends State<RecallAnalyticsPage> {
  final TextEditingController _query = TextEditingController();
  Map<String, dynamic> _data = <String, dynamic>{};
  int _selected = -1;
  int _topK = 8;
  bool _busy = false;
  String _status = '输入查询后回车，走完整六路召回 + 融合精排';
  double _costMs = 0;

  @override
  void dispose() {
    _query.dispose();
    super.dispose();
  }

  Future<void> _run() async {
    final text = _query.text.trim();
    if (text.isEmpty) return;
    setState(() => _busy = true);
    final started = DateTime.now();
    final data = await widget.client.recallDetailed(text, limit: _topK, deep: true);
    if (!mounted) return;
    final hits = data['hits'] as List<dynamic>? ?? <dynamic>[];
    setState(() {
      _data = data;
      _selected = hits.isEmpty ? -1 : 0;
      _busy = false;
      _costMs = DateTime.now().difference(started).inMilliseconds.toDouble();
      _status = data['error']?.toString() ??
          '命中 ${hits.length} 条 · $_costMs ms · 向量源 '
              '${data['degraded'] == true ? '降级（字符 n-gram）' : 'BGE-M3'} · '
              '注入约 ${(_s(data['injection']).length ~/ 2)} tokens（粗估）';
    });
  }

  @override
  Widget build(BuildContext context) {
    final hits = (_data['hits'] as List<dynamic>? ?? <dynamic>[])
        .map((row) => RecallHit.fromJson(row is Map<String, dynamic> ? row : <String, dynamic>{}))
        .toList();
    final audit = _data['audit'] is Map<String, dynamic>
        ? _data['audit'] as Map<String, dynamic>
        : <String, dynamic>{};
    final selected = _selected >= 0 && _selected < hits.length ? hits[_selected] : null;
    return PageFrame(
      title: '召回测试 · 检索即解释',
      subtitle: '六通道融合 + RRF 精排；命中列表可点选，右侧拆解「凭什么被召回」与最终注入上下文',
      child: Column(
        children: <Widget>[
          _box(<Widget>[
            Row(
              children: <Widget>[
                Expanded(
                  child: TextField(
                    controller: _query,
                    style: const TextStyle(color: Colors.white, fontSize: 13),
                    decoration: const InputDecoration(
                      hintText: '例如：发版冲突 / 年检模式',
                      hintStyle: TextStyle(color: Color(0xFF63788F)),
                      border: InputBorder.none,
                    ),
                    onSubmitted: (_) => _run(),
                  ),
                ),
                const SizedBox(width: 8),
                DropdownButton<int>(
                  value: _topK,
                  dropdownColor: _card,
                  style: const TextStyle(color: _hint, fontSize: 11),
                  underline: const SizedBox.shrink(),
                  items: <int>[5, 8, 12, 20, 50]
                      .map((value) => DropdownMenuItem<int>(value: value, child: Text('top $value')))
                      .toList(),
                  onChanged: (value) => setState(() => _topK = value ?? 8),
                ),
                const SizedBox(width: 8),
                FilledButton(onPressed: _busy ? null : _run, child: const Text('召回')),
                const SizedBox(width: 8),
                OutlinedButton(
                  onPressed: _query.text.trim().isEmpty
                      ? null
                      : () => widget.onNebulaSearch(_query.text.trim()),
                  child: const Text('在星云中激活'),
                ),
              ],
            ),
          ]),
          const SizedBox(height: 8),
          Align(
            alignment: Alignment.centerLeft,
            child: Text(_status, style: const TextStyle(color: _hint, fontSize: 11)),
          ),
          const SizedBox(height: 8),
          Expanded(
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: <Widget>[
                Expanded(
                  flex: 5,
                  child: _box(<Widget>[
                    _section('命中记忆（点击看归因）'),
                    Expanded(
                      child: hits.isEmpty
                          ? const Center(child: Text('暂无命中', style: TextStyle(color: _hint, fontSize: 11)))
                          : ListView.builder(
                              itemCount: hits.length,
                              itemBuilder: (context, index) {
                                final hit = hits[index];
                                return ListTile(
                                  selected: _selected == index,
                                  selectedTileColor: const Color(0xFF123049),
                                  dense: true,
                                  title: Text(
                                    hit.content.isEmpty ? '(空内容)' : hit.content,
                                    maxLines: 2,
                                    overflow: TextOverflow.ellipsis,
                                    style: const TextStyle(color: Colors.white, fontSize: 12),
                                  ),
                                  subtitle: Text(
                                    '${hit.memoryType} · ${hit.channels.isEmpty ? '—' : hit.channels.join('+')} '
                                    '· PAD ${hit.pad.length == 3 ? hit.pad.map((v) => v.toStringAsFixed(1)).join('/') : '—'}',
                                    style: const TextStyle(color: _hint, fontSize: 10),
                                  ),
                                  trailing: Text(hit.score.toStringAsFixed(3),
                                      style: const TextStyle(color: _accent, fontSize: 11)),
                                  onTap: () => setState(() => _selected = index),
                                );
                              },
                            ),
                    ),
                  ]),
                ),
                const SizedBox(width: 10),
                Expanded(
                  flex: 6,
                  child: SingleChildScrollView(
                    child: Column(
                      children: <Widget>[
                        _channelCard(audit),
                        const SizedBox(height: 10),
                        _whyCard(selected, audit),
                        const SizedBox(height: 10),
                        _injectionCard(_s(_data['injection'])),
                      ],
                    ),
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }

  Widget _channelCard(Map<String, dynamic> audit) {
    final counts = audit['counts'] is Map ? audit['counts'] as Map : const <String, dynamic>{};
    final injected = audit['injected'] is Map ? audit['injected'] as Map : const <String, dynamic>{};
    final weights = audit['weights'] is Map ? audit['weights'] as Map : const <String, dynamic>{};
    final maxValue = _channels
        .map((channel) => math.max(_d(counts[channel['key']]), _d(injected[channel['key']])))
        .fold<double>(1.0, (previous, value) => math.max(previous, value));
    return _box(<Widget>[
      _section('各路召回命中数（召回 → 最终注入）'),
      for (final channel in _channels)
        Padding(
          padding: const EdgeInsets.symmetric(vertical: 4),
          child: Row(
            children: <Widget>[
              SizedBox(
                width: 68,
                child: Text(_s(channel['label']), style: const TextStyle(color: _hint, fontSize: 10)),
              ),
              Expanded(
                child: Stack(
                  children: <Widget>[
                    Container(height: 8, decoration: BoxDecoration(color: const Color(0xFF142238), borderRadius: BorderRadius.circular(4))),
                    FractionallySizedBox(
                      widthFactor: (_d(counts[channel['key']]) / maxValue).clamp(0.0, 1.0),
                      child: Container(height: 8, decoration: BoxDecoration(color: channel['color']! as Color, borderRadius: BorderRadius.circular(4))),
                    ),
                    FractionallySizedBox(
                      widthFactor: (_d(injected[channel['key']]) / maxValue).clamp(0.0, 1.0),
                      child: Container(height: 3, margin: const EdgeInsets.only(top: 5), color: Colors.white),
                    ),
                  ],
                ),
              ),
              const SizedBox(width: 8),
              SizedBox(
                width: 52,
                child: Text('${_d(counts[channel['key']]).toInt()}→${_d(injected[channel['key']]).toInt()}',
                    style: const TextStyle(color: Colors.white, fontSize: 10)),
              ),
            ],
          ),
        ),
      const SizedBox(height: 8),
      Text(
        '融合权重：RRF ${_s(weights['rrf'])} / 新鲜度 ${_s(weights['freshness'])} / '
        '突触 ${_s(weights['synapse'])} / 情绪 ${_s(weights['pad'])} / 置信度 ${_s(weights['confidence'])}'
        '　·　交叉编码精排：${audit['rerank'] == true ? '已启用' : '已关闭'}',
        style: const TextStyle(color: _hint, fontSize: 10, height: 1.6),
      ),
    ]);
  }

  Widget _whyCard(RecallHit? hit, Map<String, dynamic> audit) {
    final weights = audit['weights'] is Map ? audit['weights'] as Map : const <String, dynamic>{};
    final similarities = audit['similarities'] is Map ? audit['similarities'] as Map : const <String, dynamic>{};
    final lines = <String>[
      if (hit == null) '点击左侧任意一条命中，查看它为什么被召回' else ...<String>[
        '记忆 ID　　${hit.id}',
        '最终得分　${hit.score.toStringAsFixed(4)}',
        '命中通道　${hit.channels.isEmpty ? '—' : hit.channels.join(', ')}',
        '向量余弦　${_d(similarities[hit.id]).toStringAsFixed(4)}（BGE-M3 dense）',
        'PAD　　　 ${hit.pad.length == 3 ? hit.pad.map((v) => v.toStringAsFixed(2)).join(' / ') : '—'}',
        '── 分数构成（配置权重）──',
        '多通道 RRF 融合　× ${_s(weights['rrf'])}',
        '新鲜度衰减　　　× ${_s(weights['freshness'])}',
        '突触可提取性　　× ${_s(weights['synapse'])}',
        '情绪显著度　　　× ${_s(weights['pad'])}',
        '记忆置信度　　　× ${_s(weights['confidence'])}',
        '交叉编码精排　　${audit['rerank'] == true ? '已启用' : '已关闭'}',
      ],
    ];
    return _box(<Widget>[
      _section('为什么命中'),
      SelectableText(lines.join('\n'),
          style: const TextStyle(color: Color(0xFFBFD8F5), fontSize: 11, height: 1.7)),
      if (hit != null) ...<Widget>[
        const SizedBox(height: 8),
        Align(
          alignment: Alignment.centerLeft,
          child: OutlinedButton(
            onPressed: () => widget.onOpenMemory(hit.id),
            child: const Text('在记忆检查器中打开'),
          ),
        ),
      ],
    ]);
  }

  Widget _injectionCard(String injection) => _box(<Widget>[
        Row(
          children: <Widget>[
            const Expanded(child: Text('注入上下文（L0–L4 收敛）', style: TextStyle(color: Colors.white, fontSize: 13))),
            IconButton(
              tooltip: '复制',
              icon: const Icon(Icons.copy_all_outlined, color: _hint, size: 16),
              onPressed: injection.isEmpty
                  ? null
                  : () => Clipboard.setData(ClipboardData(text: injection)),
            ),
          ],
        ),
        const SizedBox(height: 6),
        SizedBox(
          height: 150,
          child: SingleChildScrollView(
            child: SelectableText(
              injection.isEmpty ? '（无注入上下文）' : injection,
              style: const TextStyle(color: Color(0xFFBFD8F5), fontSize: 11, height: 1.6),
            ),
          ),
        ),
      ]);
}

// ─────────────────────────────────────────────────────────────────────────────
// 2. PAD 认知分区（散点 + 分类型/分层均值）
// ─────────────────────────────────────────────────────────────────────────────
class PadAnalyticsPage extends StatefulWidget {
  const PadAnalyticsPage({super.key, required this.client, required this.onOpenMemory});
  final BrainClient client;
  final OpenMemoryCallback onOpenMemory;

  @override
  State<PadAnalyticsPage> createState() => _PadAnalyticsPageState();
}

class _PadAnalyticsPageState extends State<PadAnalyticsPage> {
  Map<String, dynamic> _data = <String, dynamic>{};
  int _selected = -1;
  String _status = '加载中…';

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    final data = await widget.client.padDistribution();
    if (!mounted) return;
    setState(() {
      _data = data;
      _selected = -1;
      _status = data['error']?.toString() ?? '样本 ${_s(data['total'])} 条 · 数据来自主库 neurons 真实 PAD 字段';
    });
  }

  @override
  Widget build(BuildContext context) {
    final samples = (_data['samples'] as List<dynamic>? ?? <dynamic>[])
        .whereType<Map<String, dynamic>>()
        .toList();
    final byType = _data['by_type'] is Map<String, dynamic>
        ? _data['by_type'] as Map<String, dynamic>
        : <String, dynamic>{};
    final byTier = _data['by_tier'] is Map<String, dynamic>
        ? _data['by_tier'] as Map<String, dynamic>
        : <String, dynamic>{};
    final selected = _selected >= 0 && _selected < samples.length ? samples[_selected] : null;
    return PageFrame(
      title: 'PAD 认知分区',
      subtitle: 'P 愉悦度 × A 唤醒度散点，点色=记忆类型、点径=置信度；下方为各类型与各分层的 P/A/D 均值',
      child: ListView(
        children: <Widget>[
          Row(
            children: <Widget>[
              Text(_status, style: const TextStyle(color: _hint, fontSize: 11)),
              const Spacer(),
              OutlinedButton(onPressed: _load, child: const Text('刷新')),
            ],
          ),
          const SizedBox(height: 10),
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              Expanded(
                flex: 3,
                child: _box(<Widget>[
                  _section('P–A 情绪平面'),
                  LayoutBuilder(
                    builder: (context, constraints) => GestureDetector(
                      onTapUp: (details) => _pick(details.localPosition,
                          Size(constraints.maxWidth - 32, _plotHeight(constraints.maxWidth - 32)), samples),
                      child: SizedBox(
                        height: _plotHeight(constraints.maxWidth - 32),
                        child: CustomPaint(
                          painter: _PadScatterPainter(samples: samples, selected: _selected),
                          child: const SizedBox.expand(),
                        ),
                      ),
                    ),
                  ),
                  const SizedBox(height: 8),
                  Wrap(
                    spacing: 12,
                    children: <Widget>[
                      for (final entry in _typeColors.entries)
                        Row(mainAxisSize: MainAxisSize.min, children: <Widget>[
                          Container(width: 8, height: 8, decoration: BoxDecoration(color: entry.value, shape: BoxShape.circle)),
                          const SizedBox(width: 4),
                          Text(entry.key, style: const TextStyle(color: _hint, fontSize: 10)),
                        ]),
                    ],
                  ),
                ]),
              ),
              const SizedBox(width: 10),
              Expanded(
                flex: 2,
                child: _box(<Widget>[
                  _section('选中样本'),
                  if (selected == null)
                    const Text('点击左侧散点查看记忆内容', style: TextStyle(color: _hint, fontSize: 11))
                  else ...<Widget>[
                    Text(selected['content']?.toString() ?? '',
                        style: const TextStyle(color: Colors.white, fontSize: 12, height: 1.5)),
                    const SizedBox(height: 10),
                    Text('类型 ${_s(selected['type'])} · 分层 ${_s(selected['tier'])}',
                        style: const TextStyle(color: _hint, fontSize: 11)),
                    Text('PAD ${((selected['pad'] as List<dynamic>? ?? <dynamic>[]).map((v) => _d(v).toStringAsFixed(2)).join(' / '))}',
                        style: const TextStyle(color: _accent, fontSize: 11)),
                    Text('置信度 ${_d(selected['confidence']).toStringAsFixed(2)}',
                        style: const TextStyle(color: _hint, fontSize: 11)),
                    const SizedBox(height: 10),
                    OutlinedButton(
                      onPressed: () => widget.onOpenMemory(_s(selected['id'])),
                      child: const Text('在检查器中打开'),
                    ),
                  ],
                  const SizedBox(height: 18),
                  _section('按类型均值'),
                  _meanTable(byType),
                  const SizedBox(height: 12),
                  _section('按分层均值'),
                  _meanTable(byTier),
                ]),
              ),
            ],
          ),
        ],
      ),
    );
  }

  static double _plotHeight(double width) => math.min(420, math.max(260, width));

  void _pick(Offset local, Size size, List<Map<String, dynamic>> samples) {
    final rect = Offset.zero & size;
    var best = -1;
    var bestDistance = 14.0;
    for (var i = 0; i < samples.length; i++) {
      final pad = samples[i]['pad'] as List<dynamic>? ?? <dynamic>[0, 0, 0];
      final x = rect.center.dx + _d(pad[0]) * rect.width * 0.44;
      final y = rect.center.dy - _d(pad[1]) * rect.height * 0.44;
      final distance = (Offset(x, y) - local).distance;
      if (distance < bestDistance) {
        bestDistance = distance;
        best = i;
      }
    }
    if (best >= 0) setState(() => _selected = best);
  }

  Widget _meanTable(Map<String, dynamic> groups) {
    if (groups.isEmpty) return const Text('—', style: TextStyle(color: _hint, fontSize: 11));
    return Column(
      children: <Widget>[
        for (final entry in groups.entries)
          Builder(builder: (context) {
            final row = entry.value is Map<String, dynamic>
                ? entry.value as Map<String, dynamic>
                : <String, dynamic>{};
            return Padding(
              padding: const EdgeInsets.symmetric(vertical: 3),
              child: Row(
                children: <Widget>[
                  SizedBox(width: 84, child: Text(entry.key, style: const TextStyle(color: _hint, fontSize: 10))),
                  Expanded(
                    child: Text(
                      'P${_d(row['p']).toStringAsFixed(2)}  A${_d(row['a']).toStringAsFixed(2)}  '
                      'D${_d(row['d']).toStringAsFixed(2)}',
                      style: const TextStyle(color: Colors.white, fontSize: 10),
                    ),
                  ),
                  Text('×${_d(row['count']).toInt()}', style: const TextStyle(color: _accent, fontSize: 10)),
                ],
              ),
            );
          }),
      ],
    );
  }
}

class _PadScatterPainter extends CustomPainter {
  const _PadScatterPainter({required this.samples, required this.selected});
  final List<Map<String, dynamic>> samples;
  final int selected;

  @override
  void paint(Canvas canvas, Size size) {
    final rect = Offset.zero & size;
    final axis = Paint()..color = const Color(0xFF1C3350)..strokeWidth = 1;
    canvas.drawLine(Offset(rect.center.dx, 0), Offset(rect.center.dx, size.height), axis);
    canvas.drawLine(Offset(0, rect.center.dy), Offset(size.width, rect.center.dy), axis);
    for (var i = 1; i <= 4; i++) {
      final r = rect.width * 0.44 * (i / 4);
      canvas.drawCircle(rect.center, r, Paint()..color = const Color(0xFF142238)..style = PaintingStyle.stroke);
    }
    for (var i = 0; i < samples.length; i++) {
      final row = samples[i];
      final pad = row['pad'] as List<dynamic>? ?? <dynamic>[0, 0, 0];
      if (pad.length < 2) continue;
      final x = rect.center.dx + _d(pad[0]).clamp(-1.0, 1.0) * rect.width * 0.44;
      final y = rect.center.dy - _d(pad[1]).clamp(-1.0, 1.0) * rect.height * 0.44;
      final radius = 2.5 + _d(row['confidence']).clamp(0.0, 1.0) * 3.5;
      final chosen = i == selected;
      canvas.drawCircle(
        Offset(x, y),
        radius,
        Paint()..color = _colorOf(_s(row['type'])),
      );
      if (chosen) {
        canvas.drawCircle(Offset(x, y), radius + 5,
            Paint()..color = Colors.white..style = PaintingStyle.stroke..strokeWidth = 1.5);
      }
    }
    final labels = <(String, Offset)>[
      ('+P 愉悦', Offset(size.width - 46, rect.center.dy - 12)),
      ('-P', Offset(6, rect.center.dy - 12)),
      ('+A 唤醒', Offset(rect.center.dx + 6, 4)),
      ('-A', Offset(rect.center.dx + 6, size.height - 16)),
    ];
    for (final label in labels) {
      final painter = TextPainter(
        text: TextSpan(text: label.$1, style: const TextStyle(color: Color(0xFF5D7690), fontSize: 9)),
        textDirection: TextDirection.ltr,
      )..layout();
      painter.paint(canvas, label.$2);
    }
  }

  @override
  bool shouldRepaint(_PadScatterPainter old) =>
      old.selected != selected || old.samples.length != samples.length;
}

// ─────────────────────────────────────────────────────────────────────────────
// 3. 2D 语义环检查器（与 3D 星云共享同一份 neurons，支持跳转检查器 / 星云定位）
// ─────────────────────────────────────────────────────────────────────────────
class Graph2DPage extends StatefulWidget {
  const Graph2DPage({
    super.key,
    required this.client,
    required this.onOpenMemory,
    required this.onNebulaSearch,
  });
  final BrainClient client;
  final OpenMemoryCallback onOpenMemory;
  final NebulaSearchCallback onNebulaSearch;

  @override
  State<Graph2DPage> createState() => _Graph2DPageState();
}

class _Graph2DPageState extends State<Graph2DPage> {
  Map<String, dynamic> _data = <String, dynamic>{};
  Map<String, dynamic>? _selected;
  String _status = '加载 2D 语义环投影…';

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    final data = await widget.client.graphProjection();
    if (!mounted) return;
    final nodes = data['nodes'] as List<dynamic>? ?? <dynamic>[];
    setState(() {
      _data = data;
      _selected = null;
      _status = data['error']?.toString() ??
          '${nodes.length} 个节点 · ${(data['edges'] as List<dynamic>? ?? <dynamic>[]).length} 条关系 '
          '· 环=分层，扇区=记忆类型，扇区内角度=形成时间';
    });
  }

  void _pick(Offset local, Size size) {
    final nodes = (_data['nodes'] as List<dynamic>? ?? <dynamic>[]).whereType<Map<String, dynamic>>();
    final scale = math.min(size.width, size.height) / 2 / 340;
    final center = Offset(size.width / 2, size.height / 2);
    Map<String, dynamic>? best;
    var bestDistance = 12.0;
    for (final node in nodes) {
      final point = center + Offset(_d(node['x']) * scale, _d(node['y']) * scale);
      final distance = (point - local).distance;
      if (distance < bestDistance) {
        bestDistance = distance;
        best = node;
      }
    }
    setState(() => _selected = best);
  }

  @override
  Widget build(BuildContext context) {
    final nodes = (_data['nodes'] as List<dynamic>? ?? <dynamic>[]).whereType<Map<String, dynamic>>().toList();
    final edges = (_data['edges'] as List<dynamic>? ?? <dynamic>[]).whereType<Map<String, dynamic>>().toList();
    final selected = _selected;
    return PageFrame(
      title: '2D 语义环检查器',
      subtitle: 'Qt 2D 可分析控制台的等价视图：确定性语义布局 + 节点标签 + 关系连线；点击节点可联动检查器与 3D 星云',
      child: Column(
        children: <Widget>[
          Row(
            children: <Widget>[
              Expanded(child: Text(_status, style: const TextStyle(color: _hint, fontSize: 11))),
              OutlinedButton(onPressed: _load, child: const Text('刷新')),
            ],
          ),
          const SizedBox(height: 10),
          Expanded(
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: <Widget>[
                Expanded(
                  flex: 3,
                  child: _box(<Widget>[
                    Expanded(
                      child: LayoutBuilder(
                        builder: (context, constraints) => GestureDetector(
                          onTapUp: (details) =>
                              _pick(details.localPosition, Size(constraints.maxWidth, constraints.maxHeight)),
                          child: SizedBox.expand(
                            child: CustomPaint(
                              painter: _SemanticRingPainter(nodes: nodes, edges: edges, selectedId: selected?['id']?.toString()),
                            ),
                          ),
                        ),
                      ),
                    ),
                    const SizedBox(height: 8),
                    Wrap(
                      spacing: 12,
                      children: <Widget>[
                        for (final entry in _typeColors.entries)
                          Row(mainAxisSize: MainAxisSize.min, children: <Widget>[
                            Container(width: 8, height: 8, decoration: BoxDecoration(color: entry.value, shape: BoxShape.circle)),
                            const SizedBox(width: 4),
                            Text(entry.key, style: const TextStyle(color: _hint, fontSize: 10)),
                          ]),
                        const Text('内环 core · 中环 buffer · 外环 archive/percept',
                            style: TextStyle(color: _hint, fontSize: 10)),
                      ],
                    ),
                  ]),
                ),
                const SizedBox(width: 10),
                Expanded(
                  flex: 2,
                  child: _box(<Widget>[
                    _section('节点详情'),
                    if (selected == null)
                      const Text('点击左侧任意节点，查看结构化摘要与联动操作',
                          style: TextStyle(color: _hint, fontSize: 11))
                    else ...<Widget>[
                      SelectableText(selected['content']?.toString() ?? '',
                          style: const TextStyle(color: Colors.white, fontSize: 12, height: 1.6)),
                      const SizedBox(height: 10),
                      Text('ID ${_s(selected['id'])}', style: const TextStyle(color: _hint, fontSize: 10)),
                      Text('类型 ${_s(selected['type'])} · 分层 ${_s(selected['tier'])}',
                          style: const TextStyle(color: _hint, fontSize: 11)),
                      Text('置信度 ${_d(selected['confidence']).toStringAsFixed(2)}',
                          style: const TextStyle(color: _hint, fontSize: 11)),
                      Text(
                        'PAD ${((selected['pad'] as List<dynamic>? ?? <dynamic>[]).map((v) => _d(v).toStringAsFixed(2)).join(' / '))}',
                        style: const TextStyle(color: _accent, fontSize: 11),
                      ),
                      const SizedBox(height: 12),
                      Wrap(
                        spacing: 8,
                        runSpacing: 8,
                        children: <Widget>[
                          OutlinedButton(
                            onPressed: () => widget.onOpenMemory(_s(selected['id'])),
                            child: const Text('记忆检查器'),
                          ),
                          OutlinedButton(
                            onPressed: () => widget.onNebulaSearch(
                                (selected['content']?.toString() ?? '').trim().split(RegExp(r'\s+')).take(4).join(' ')),
                            child: const Text('3D 星云定位'),
                          ),
                        ],
                      ),
                    ],
                    const Spacer(),
                    const SizedBox(height: 12),
                    const Text('联动说明：2D 与 3D 星云读取同一份主库 neurons，'
                        '坐标算法与 Qt 语义环一致（同 ID 必在同位置），因此可在两视图与检查器之间自由跳转。',
                        style: TextStyle(color: Color(0xFF5D7690), fontSize: 10, height: 1.6)),
                  ]),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _SemanticRingPainter extends CustomPainter {
  const _SemanticRingPainter({required this.nodes, required this.edges, this.selectedId});
  final List<Map<String, dynamic>> nodes;
  final List<Map<String, dynamic>> edges;
  final String? selectedId;

  @override
  void paint(Canvas canvas, Size size) {
    final center = Offset(size.width / 2, size.height / 2);
    final scale = math.min(size.width, size.height) / 2 / 340;
    final ring = Paint()..color = const Color(0xFF142238)..style = PaintingStyle.stroke..strokeWidth = 1;
    for (final radius in <double>[60, 145, 245, 325]) {
      canvas.drawCircle(center, radius * scale, ring);
    }
    final sectors = 5;
    for (var i = 0; i < sectors; i++) {
      final angle = i * 2 * math.pi / sectors;
      canvas.drawLine(
        center,
        center + Offset(math.cos(angle), math.sin(angle)) * 325 * scale,
        ring,
      );
    }
    final byId = <String, Offset>{
      for (final node in nodes)
        _s(node['id']): center + Offset(_d(node['x']) * scale, _d(node['y']) * scale),
    };
    final edgePaint = Paint()..color = const Color(0xFF1C3350)..strokeWidth = 0.6;
    for (final edge in edges) {
      final from = byId[_s(edge['source'])];
      final to = byId[_s(edge['target'])];
      if (from != null && to != null) canvas.drawLine(from, to, edgePaint);
    }
    final labelStyle = const TextStyle(color: Color(0xFF8FA6C4), fontSize: 9);
    var labeled = 0;
    for (final node in nodes) {
      final point = byId[_s(node['id'])];
      if (point == null) continue;
      final emotion = node['is_emotion'] == true;
      final chosen = selectedId != null && selectedId == _s(node['id']);
      final radius = (chosen ? 6.0 : (emotion ? 4.0 : 2.8)) * scale.clamp(0.6, 1.4);
      canvas.drawCircle(
        point,
        radius,
        Paint()..color = emotion ? const Color(0xFFFF8FB1) : _colorOf(_s(node['type'])),
      );
      if (chosen) {
        canvas.drawCircle(point, radius + 7,
            Paint()..color = Colors.white..style = PaintingStyle.stroke..strokeWidth = 1.5);
      }
      // 只给高置信度节点画标签，避免 3000+ 节点糊成一片（与 Qt 的标签优先级策略一致）
      if (labeled < 45 && (_d(node['confidence']) > 0.72 || chosen)) {
        final text = '${_s(node['type'])} ● ${_s(node['content']).substring(0, math.min(12, _s(node['content']).length))}';
        final painter = TextPainter(
          text: TextSpan(text: text, style: labelStyle),
          textDirection: TextDirection.ltr,
        )..layout();
        painter.paint(canvas, point + Offset(radius + 2, -radius - 4));
        labeled++;
      }
    }
  }

  @override
  bool shouldRepaint(_SemanticRingPainter old) =>
      old.selectedId != selectedId || old.nodes.length != nodes.length;
}
