import 'dart:convert';

import 'package:flutter/material.dart';

import 'brain_client.dart';
import 'pages.dart' show PageFrame;
import 'persona_whale.dart';

const Color _accentP = Color(0xFF4FC3F7);
const Color _hintP = Color(0xFF8FA6C4);

/// L0 人格层：骨架只读 + 可调参数手动微调 + 反馈演化提案 + 版本回滚。
///
/// 全部读写走后端 RPC（persona.* / topics.list / graph.real_edges），
/// 不直连数据库；骨架字段任何反馈都不可改，参数每次变更都落版本可回滚。
class PersonaPage extends StatefulWidget {
  const PersonaPage({super.key, required this.client});
  final BrainClient client;

  @override
  State<PersonaPage> createState() => _PersonaPageState();
}

class _PersonaPageState extends State<PersonaPage> {
  Map<String, dynamic> _data = <String, dynamic>{};
  Map<String, dynamic> _draft = <String, dynamic>{};
  Map<String, dynamic> _topics = <String, dynamic>{};
  Map<String, dynamic> _edges = <String, dynamic>{};
  List<dynamic> _history = <dynamic>[];
  List<dynamic> _proposals = <dynamic>[];
  String _baseline = '';
  String _status = '—';
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _reload();
  }

  Future<void> _reload() async {
    try {
      final results = await Future.wait<dynamic>(<Future<dynamic>>[
        widget.client.personaGet(),
        widget.client.personaProposals(),
        widget.client.topicsList(),
        widget.client.realEdges(limit: 12),
      ]);
      final data = (results[0] as Map).cast<String, dynamic>();
      final proposals = (results[1] as Map).cast<String, dynamic>();
      final topics = (results[2] as Map).cast<String, dynamic>();
      final edges = (results[3] as Map).cast<String, dynamic>();
      final params = (data['params'] is Map)
          ? (data['params'] as Map).cast<String, dynamic>()
          : <String, dynamic>{};
      final emo = (params['emotion_baseline'] is Map)
          ? (params['emotion_baseline'] as Map).cast<String, dynamic>()
          : <String, dynamic>{'p': 0.2, 'a': 0.45, 'd': 0.5};
      if (!mounted) return;
      setState(() {
        _data = data;
        _draft = <String, dynamic>{...params, 'emotion_baseline': <String, dynamic>{...emo}};
        _history = (data['history'] is List<dynamic>)
            ? List<dynamic>.from(data['history'] as List<dynamic>).reversed.toList()
            : <dynamic>[];
        _proposals = (proposals['items'] is List<dynamic>)
            ? List<dynamic>.from(proposals['items'] as List<dynamic>).reversed.toList()
            : <dynamic>[];
        _topics = topics;
        _edges = edges;
        _loading = false;
        _status = '就绪 · 版本 ${data['history_total'] ?? 0} · 待确认提案 ${proposals['pending'] ?? 0}';
      });
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _status = '读取失败：$error';
      });
    }
  }

  double _n(String key, double fallback, {String? group}) {
    final src = group == null ? _draft : _draft[group];
    final v = src is Map ? src[key] : null;
    return v is num ? v.toDouble() : fallback;
  }

  void _set(String key, double value, {String? group, bool integer = false}) {
    setState(() {
      final out = integer ? value.round() : value;
      if (group == null) {
        _draft[key] = out;
      } else {
        final m = <String, dynamic>{};
        final old = _draft[group];
        if (old is Map) m.addAll(old.cast<String, dynamic>());
        m[key] = out;
        _draft[group] = m;
      }
    });
  }

  bool _same(dynamic a, dynamic b) {
    try {
      return jsonEncode(a) == jsonEncode(b);
    } catch (_) {
      return a == b;
    }
  }

  Future<void> _save() async {
    final old = (_data['params'] is Map)
        ? (_data['params'] as Map).cast<String, dynamic>()
        : <String, dynamic>{};
    final patch = <String, dynamic>{};
    _draft.forEach((String key, dynamic value) {
      if (!_same(value, old[key])) patch[key] = value;
    });
    if (patch.isEmpty) {
      setState(() => _status = '没有变更');
      return;
    }
    final result = await widget.client.personaSetParams(patch);
    if (!mounted) return;
    setState(() => _status = result['ok'] == true
        ? '已保存 v${result['version']}（${patch.length} 项变更）'
        : '保存失败：${result['error']}');
    await _reload();
  }

  Future<void> _rollback(int version) async {
    final r = await widget.client.personaRollback(version);
    if (!mounted) return;
    setState(() => _status = r['ok'] == true ? '已回滚到 v$version' : '回滚失败：${r['error']}');
    await _reload();
  }

  Future<void> _applyProposal(String id) async {
    final r = await widget.client.personaApplyProposal(id);
    if (!mounted) return;
    setState(() => _status = r['ok'] == true ? '提案已应用 v${r['version']}' : '应用失败：${r['error']}');
    await _reload();
  }

  Future<void> _feedback(double valence, String signal) async {
    final r = await widget.client.personaFeedback(
      signal: signal,
      valence: valence,
      target: valence > 0 ? 'clinginess' : 'sassiness',
      sessionId: 'flutter-${DateTime.now().millisecondsSinceEpoch ~/ 60000}',
    );
    if (!mounted) return;
    setState(() => _status = r['proposal'] != null
        ? '已生成人格提案（同向 3 次且跨会话 ≥2）'
        : '反馈已记录：${r['note']}');
    await _reload();
  }

  /// 骨架编辑（全系统唯一写入口）：显式授权短语 + 旧全文自动备份。
  Future<void> _editBaseline() async {
    if (_baseline.isEmpty) {
      _baseline = await widget.client.personaBaseline();
    }
    if (!mounted) return;
    final content = TextEditingController(text: _baseline);
    final confirm = TextEditingController();
    String status = '';
    final ok = await showDialog<bool>(
      context: context,
      builder: (BuildContext ctx) => StatefulBuilder(builder: (ctx, setDialog) => AlertDialog(
        backgroundColor: const Color(0xFF0A1424),
        title: const Text('编辑人格骨架（唯一修改入口）',
            style: TextStyle(color: Colors.white, fontSize: 15)),
        content: SizedBox(
          width: 640,
          height: 520,
          child: Column(children: <Widget>[
            Expanded(child: TextField(
              controller: content,
              maxLines: null,
              expands: true,
              textAlignVertical: TextAlignVertical.top,
              style: const TextStyle(color: Colors.white, fontSize: 11.5, fontFamily: 'monospace'),
              decoration: InputDecoration(
                filled: true, fillColor: const Color(0xFF0E1B2D),
                border: OutlineInputBorder(borderRadius: BorderRadius.circular(6), borderSide: BorderSide.none),
              ),
            )),
            const SizedBox(height: 10),
            TextField(
              controller: confirm,
              obscureText: true,
              style: const TextStyle(color: Colors.white, fontSize: 12),
              decoration: InputDecoration(
                hintText: '授权短语：确认修改人格骨架',
                hintStyle: const TextStyle(color: Color(0xFF63788F), fontSize: 11),
                isDense: true, filled: true, fillColor: const Color(0xFF0E1B2D),
                border: OutlineInputBorder(borderRadius: BorderRadius.circular(6), borderSide: BorderSide.none),
              ),
            ),
            const SizedBox(height: 8),
            if (status.isNotEmpty)
              Text(status, style: const TextStyle(color: Color(0xFFFFB74D), fontSize: 10.5)),
          ]),
        ),
        actions: <Widget>[
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('取消')),
          FilledButton(
            onPressed: () async {
              setDialog(() => status = '写入中…');
              try {
                final r = await widget.client.call('persona.baseline.write', <String, dynamic>{
                  'content': content.text,
                  'confirm': confirm.text.trim(),
                  'actor': 'frontend',
                });
                final map = (r is Map) ? r : <String, dynamic>{};
                if (map['ok'] == true) {
                  if (mounted) {
                    _baseline = content.text;
                    _reload();
                  }
                  if (ctx.mounted) Navigator.pop(ctx, true);
                } else {
                  setDialog(() => status = '失败：${map['error'] ?? '未知错误'}');
                }
              } catch (e) {
                setDialog(() => status = '失败：$e');
              }
            },
            child: const Text('授权写入'),
          ),
        ],
      )),
    );
    if (ok == true && mounted) {
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(
          content: Text('骨架已更新（旧全文已自动备份到项目档案）'), backgroundColor: Color(0xFF16351F)));
    }
  }

  Future<void> _showBaseline() async {
    if (_baseline.isEmpty) {
      _baseline = await widget.client.personaBaseline();
    }
    if (!mounted) return;
    await showDialog<void>(
      context: context,
      builder: (BuildContext ctx) => AlertDialog(
        backgroundColor: const Color(0xFF0A1424),
        title: const Text('人格基线骨架（只读）',
            style: TextStyle(color: Colors.white, fontSize: 15)),
        content: SizedBox(
          width: 620,
          height: 460,
          child: SingleChildScrollView(
            child: Text(_baseline.isEmpty ? '（未取到全文）' : _baseline,
                style: const TextStyle(color: Color(0xFFB9CFE8), fontSize: 11, height: 1.6)),
          ),
        ),
        actions: <Widget>[
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(),
            child: const Text('关闭', style: TextStyle(color: _accentP)),
          ),
        ],
      ),
    );
  }

  Widget _box(Widget child) => Container(
        width: double.infinity,
        padding: const EdgeInsets.all(14),
        margin: const EdgeInsets.only(bottom: 12),
        decoration: BoxDecoration(
          color: const Color(0xFF0A1424),
          borderRadius: BorderRadius.circular(12),
          border: Border.all(color: const Color(0xFF1B2A42)),
        ),
        child: child,
      );

  Widget _head(String text) => Padding(
        padding: const EdgeInsets.only(bottom: 10),
        child: Text(text,
            style: const TextStyle(
                color: Colors.white, fontSize: 13, fontWeight: FontWeight.w600)),
      );

  Widget _slider(String label, String key, double min, double max,
      double fallback, {String? group, bool integer = false}) {
    final v = _n(key, fallback, group: group);
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 2),
      child: Row(children: <Widget>[
        SizedBox(
          width: 96,
          child: Text(label,
              style: const TextStyle(color: _hintP, fontSize: 10)),
        ),
        Expanded(
          child: Slider(
            value: v.clamp(min, max).toDouble(),
            min: min,
            max: max,
            divisions: integer ? (max - min).round() : 20,
            activeColor: _accentP,
            inactiveColor: const Color(0xFF1B2A42),
            onChanged: (double x) => _set(key, x, group: group, integer: integer),
          ),
        ),
        SizedBox(
          width: 34,
          child: Text(integer ? v.round().toString() : v.toStringAsFixed(2),
              style: const TextStyle(color: Colors.white, fontSize: 10)),
        ),
      ]),
    );
  }

  Widget _toggle(String label, String key) {
    final v = _draft[key] != false;
    return SwitchListTile(
      dense: true,
      contentPadding: EdgeInsets.zero,
      title: Text(label, style: const TextStyle(color: _hintP, fontSize: 11)),
      value: v,
      activeThumbColor: _accentP,
      onChanged: (bool x) => setState(() => _draft[key] = x),
    );
  }

  Widget _left() => Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          _box(Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              _head('情绪基线 PAD（本地预览，点保存才落库）'),
              _slider('愉悦 P', 'p', -1, 1, 0.2, group: 'emotion_baseline'),
              _slider('唤醒 A', 'a', -1, 1, 0.45, group: 'emotion_baseline'),
              _slider('支配 D', 'd', -1, 1, 0.5, group: 'emotion_baseline'),
            ],
          )),
          _box(Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              _head('骨架只读层（任何反馈都不可改）'),
              Text(
                '基线档案 #${_data['baseline']?['id'] ?? '—'} · '
                '${_data['baseline']?['title'] ?? '—'} · '
                '${_data['baseline']?['bytes'] ?? 0} 字节',
                style: const TextStyle(color: _hintP, fontSize: 10),
              ),
              const SizedBox(height: 6),
              Wrap(
                spacing: 5,
                runSpacing: 4,
                children: <Widget>[
                  for (final k in (_data['skeleton_keys'] is List<dynamic>
                      ? _data['skeleton_keys'] as List<dynamic>
                      : <dynamic>[]))
                    Container(
                      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 3),
                      decoration: BoxDecoration(
                        color: const Color(0x143B82F6),
                        borderRadius: BorderRadius.circular(4),
                      ),
                      child: Text('$k · 只读',
                          style: const TextStyle(color: Color(0xFF9CC7FF), fontSize: 9)),
                    ),
                ],
              ),
              const SizedBox(height: 8),
              Row(children: <Widget>[
                OutlinedButton(
                  onPressed: _showBaseline,
                  child: const Text('查看骨架全文', style: TextStyle(fontSize: 11)),
                ),
                const SizedBox(width: 8),
                FilledButton.tonal(
                  onPressed: _editBaseline,
                  child: const Text('编辑骨架', style: TextStyle(fontSize: 11)),
                ),
              ]),
            ],
          )),
          _box(Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              _head('护栏'),
              const Text(
                '① 全量可读 ≠ 可写人格：persona 层写入需 persona_write 显式授权\n'
                '② 骨架字段任何反馈不可改；自动微调单次 ≤0.05 且需同向 3 次、跨会话 ≥2\n'
                '③ sensitive 记忆不外发、不进人格层\n'
                '④ 执行权限归 WorkBuddy，不因人格演化放开\n'
                '⑤ 闲聊防 OOC 生效；主人指令走指令通道，不装傻不挡活',
                style: TextStyle(color: _hintP, fontSize: 10, height: 1.7),
              ),
            ],
          )),
        ],
      );

  Widget _right() => Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          _box(Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              _head('人格意象 · 在记忆星点间遨游（随参数实时变化）'),
              PersonaWhale(params: _draft, height: 250),
              const SizedBox(height: 8),
              const Text(
                '愉悦度→水色/腮红/微笑  唤醒度→尾鳍摆动/气泡  支配度→体型/光晕  '
                '黏人→伴飞记忆光点  斗嘴→眉角  方言→气泡',
                style: TextStyle(color: _hintP, fontSize: 9, height: 1.5),
              ),
            ],
          )),
          _box(Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              _head('可调参数 · 手动微调'),
              _slider('斗嘴强度', 'sassiness', 0, 1, 0.6),
              _slider('黏人程度', 'clinginess', 0, 1, 0.6),
              _slider('吃醋阈值', 'jealousy', 0, 1, 0.5),
              _slider('主动关心', 'care_initiative', 0, 1, 0.5),
              _slider('方言句/天', 'dialect_per_day', 0, 30, 15, integer: true),
              _slider('最大回复条数', 'max_lines_per_reply', 1, 6, 6, integer: true),
              const Padding(
                padding: EdgeInsets.only(left: 2, bottom: 6),
                child: Text(
                  '只是硬上限，不是每轮目标：设 3 则按语境回 1~3 条，设 5 则回 1~5 条，\n'
                  '绝大多数时候会少于上限，不会每次都发满。',
                  style: TextStyle(color: _hintP, fontSize: 9, height: 1.5),
                ),
              ),
              _toggle('深夜催睡', 'sleep_nag'),
              _toggle('签名自动改', 'signature_auto'),
              _toggle('指令通道（不装傻）', 'command_channel'),
              const SizedBox(height: 8),
              Row(children: <Widget>[
                ElevatedButton(
                  onPressed: _save,
                  child: const Text('保存微调', style: TextStyle(fontSize: 11)),
                ),
                const SizedBox(width: 10),
                OutlinedButton(
                  onPressed: _reload,
                  child: const Text('刷新', style: TextStyle(fontSize: 11)),
                ),
                const SizedBox(width: 10),
                Expanded(
                  child: Text(_status,
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(color: _accentP, fontSize: 10)),
                ),
              ]),
            ],
          )),
          _box(Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              _head('正 / 负反馈 → 受控演化提案'),
              Row(children: <Widget>[
                ElevatedButton.icon(
                  onPressed: () => _feedback(0.8, '主人夸了'),
                  icon: const Icon(Icons.thumb_up_outlined, size: 13),
                  label: const Text('夸一句 +', style: TextStyle(fontSize: 10)),
                ),
                const SizedBox(width: 8),
                ElevatedButton.icon(
                  onPressed: () => _feedback(-0.8, '主人嫌弃'),
                  icon: const Icon(Icons.thumb_down_outlined, size: 13),
                  label: const Text('怼一句 −', style: TextStyle(fontSize: 10)),
                ),
              ]),
              const SizedBox(height: 8),
              if (_proposals.isEmpty)
                const Text('暂无提案（需同向连续 3 次且跨会话 ≥2）',
                    style: TextStyle(color: _hintP, fontSize: 10))
              else
                for (final raw in _proposals.take(6))
                  _proposalRow(raw is Map ? raw.cast<String, dynamic>() : <String, dynamic>{}),
            ],
          )),
          _box(Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              _head('版本历史 · 一键回滚'),
              if (_history.isEmpty)
                const Text('尚无变更版本',
                    style: TextStyle(color: _hintP, fontSize: 10))
              else
                for (final raw in _history.take(8))
                  _historyRow(raw is Map ? raw.cast<String, dynamic>() : <String, dynamic>{}),
            ],
          )),
          _box(Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              _head('主题分区分布（空间=标签，Bot 全量可读）'),
              Text(
                (_topics['by_namespace'] is Map)
                    ? (_topics['by_namespace'] as Map)
                        .entries
                        .map((e) => '${e.key}: ${e.value}')
                        .join('   ')
                    : '—',
                style: const TextStyle(color: _hintP, fontSize: 10),
              ),
              const SizedBox(height: 8),
              _head('真实关联边（Kùzu，带方向与谓词）'),
              Text(
                'RELATES ${_edges['relates'] ?? 0} · MEMBER ${_edges['member'] ?? 0}',
                style: const TextStyle(color: _accentP, fontSize: 10),
              ),
              const SizedBox(height: 5),
              for (final raw in ((_edges['edges'] is List<dynamic>)
                      ? (_edges['edges'] as List<dynamic>).take(6)
                      : <dynamic>[]))
                if (raw is Map)
                  Text(
                    '${raw['source_name']} —${raw['predicate']}→ ${raw['target_name']}',
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(color: _hintP, fontSize: 9),
                  ),
            ],
          )),
        ],
      );

  Widget _historyRow(Map<String, dynamic> row) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 3),
        child: Row(children: <Widget>[
          SizedBox(
            width: 44,
            child: Text('v${row['version'] ?? '?'}',
                style: const TextStyle(color: Colors.white, fontSize: 10)),
          ),
          Expanded(
            child: Text('${row['reason'] ?? row['source'] ?? '—'}',
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(color: _hintP, fontSize: 10)),
          ),
          TextButton(
            onPressed: row['version'] is num
                ? () => _rollback((row['version'] as num).toInt())
                : null,
            child: const Text('回滚', style: TextStyle(fontSize: 10)),
          ),
        ]),
      );

  Widget _proposalRow(Map<String, dynamic> row) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 3),
        child: Row(children: <Widget>[
          Expanded(
            child: Text(
              '${row['target'] ?? '?'}  ${row['from']} → ${row['to']}  [${row['status']}]',
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(color: _hintP, fontSize: 10),
            ),
          ),
          TextButton(
            onPressed: row['status'] == 'pending' && row['id'] != null
                ? () => _applyProposal(row['id'].toString())
                : null,
            child: const Text('应用', style: TextStyle(fontSize: 10)),
          ),
        ]),
      );

  @override
  Widget build(BuildContext context) => PageFrame(
        title: 'L0 人格层 · 大肥鱼',
        subtitle: '骨架只读 / 可调参数手动微调 / 反馈演化提案 / 版本回滚（全部经后端 RPC）',
        child: _loading
            ? const Center(child: CircularProgressIndicator())
            : SingleChildScrollView(
                child: LayoutBuilder(
                  builder: (BuildContext ctx, BoxConstraints constraints) {
                    final wide = constraints.maxWidth > 900;
                    final left = _left();
                    final right = _right();
                    return wide
                        ? Row(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: <Widget>[
                              Expanded(child: left),
                              const SizedBox(width: 14),
                              Expanded(child: right),
                            ],
                          )
                        : Column(children: <Widget>[
                            left,
                            const SizedBox(height: 12),
                            right,
                          ]);
                  },
                ),
              ),
      );
}
