import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'backend_launcher.dart';
import 'brain_client.dart';
import 'config_docs.dart';
import 'pages.dart' show PageFrame;
import 'prefs.dart';

const Color _accent = Color(0xFF4FC3F7);
const Color _hint = Color(0xFF8FA6C4);

class _Card extends StatelessWidget {
  const _Card({required this.child});
  final Widget child;

  @override
  Widget build(BuildContext context) => Container(
        width: double.infinity,
        padding: const EdgeInsets.all(16),
        decoration: BoxDecoration(
          color: const Color(0xFF0A1424),
          borderRadius: BorderRadius.circular(12),
          border: Border.all(color: const Color(0xFF1B2A42)),
        ),
        child: child,
      );
}

Widget _title(String text) => Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: Text(text, style: const TextStyle(color: Colors.white, fontSize: 14, fontWeight: FontWeight.w600)),
    );

Widget _muted(String text) => Text(text, style: const TextStyle(color: _hint, fontSize: 11, height: 1.5));

// ─────────────────────────────────────────────────────────────────────────────
// 1. 记忆检查器 + 用户控制闭环（纠正 / 遗忘 / 封存 / 敏感 / 审计）
// ─────────────────────────────────────────────────────────────────────────────
class MemoryInspectorPage extends StatefulWidget {
  /// [initialId] 由 2D 检查器 / 召回归因面板跳转时传入，实现跨页选中联动。
  const MemoryInspectorPage({super.key, required this.client, this.initialId = ''});
  final BrainClient client;
  final String initialId;

  @override
  State<MemoryInspectorPage> createState() => _MemoryInspectorPageState();
}

class _MemoryInspectorPageState extends State<MemoryInspectorPage> {
  List<Map<String, dynamic>> _rows = <Map<String, dynamic>>[];
  Map<String, dynamic>? _detail;
  String _message = '点击左侧任意记忆，查看来源、双时态、置信度与审计轨迹';
  bool _busy = false;
  final _keyword = TextEditingController();
  final _correct = TextEditingController();
  final _reason = TextEditingController();

  @override
  void initState() {
    super.initState();
    _load();
    if (widget.initialId.isNotEmpty) unawaited(_select(widget.initialId));
  }

  @override
  void dispose() {
    _keyword.dispose();
    _correct.dispose();
    _reason.dispose();
    super.dispose();
  }

  Future<void> _load({bool full = false}) async {
    setState(() => _busy = true);
    await widget.client.syncMemories(full: full);
    // 关键词过滤在前端缓存上做，命中即出，不回后端（缓存已是全量）。
    final kw = _keyword.text.trim().toLowerCase();
    final all = widget.client.memCacheView;
    final rows = kw.isEmpty
        ? all
        : all.where((Map<String, dynamic> r) =>
            (r['content']?.toString() ?? '').toLowerCase().contains(kw)).toList();
    if (!mounted) return;
    setState(() {
      _rows = rows;
      _busy = false;
      _message = '共 ${rows.length} 条记忆';
    });
  }

  Future<void> _select(String id) async {
    setState(() => _busy = true);
    final detail = await widget.client.inspectMemory(id);
    if (!mounted) return;
    setState(() {
      _detail = detail;
      _correct.text = detail['content']?.toString() ?? '';
      _busy = false;
      _message = detail['error']?.toString() ?? '已载入 $id';
    });
  }

  Future<void> _run(Future<Map<String, dynamic>> action) async {
    setState(() => _busy = true);
    final result = await action;
    if (!mounted) return;
    setState(() {
      _busy = false;
      _message = result['message']?.toString() ?? result['error']?.toString() ?? result.toString();
    });
    await _load(full: true);
    if (_detail != null && _detail!['id'] != null) {
      await _select(_detail!['id'].toString());
    }
  }

  @override
  Widget build(BuildContext context) {
    final detail = _detail ?? <String, dynamic>{};
    final audit = detail['audit'] as List<dynamic>? ?? <dynamic>[];
    final breakdown = detail['confidence_breakdown'] as Map<String, dynamic>? ?? <String, dynamic>{};
    final related = detail['related'] as List<dynamic>? ?? <dynamic>[];
    final id = detail['id']?.toString() ?? '';
    return PageFrame(
      title: '记忆检查器 · 用户控制',
      subtitle: '纠正产生新版本并保留原值；遗忘只移出推理路径，审计永远可查',
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          SizedBox(
            width: 320,
            child: Column(
              children: <Widget>[
                Row(children: <Widget>[
                  Expanded(
                    child: TextField(
                      controller: _keyword,
                      style: const TextStyle(color: Colors.white, fontSize: 12),
                      decoration: const InputDecoration(hintText: '关键词过滤…', hintStyle: TextStyle(color: _hint, fontSize: 11)),
                      onSubmitted: (_) => _load(),
                    ),
                  ),
                  IconButton(icon: const Icon(Icons.refresh, color: _accent, size: 18), onPressed: _busy ? null : _load),
                ]),
                const SizedBox(height: 8),
                Expanded(
                  child: ListView.builder(
                    itemCount: _rows.length,
                    itemBuilder: (context, index) {
                      final row = _rows[index];
                      final rowId = row['id']?.toString() ?? '';
                      return ListTile(
                        dense: true,
                        selected: rowId == id,
                        selectedTileColor: const Color(0xFF123049),
                        title: Text(row['content']?.toString() ?? '', maxLines: 2,
                            overflow: TextOverflow.ellipsis, style: const TextStyle(color: Colors.white, fontSize: 11)),
                        subtitle: Text('${row['memory_type'] ?? 'fact'} · ${row['tier'] ?? 'buffer'} · ${rowId.substring(0, rowId.length > 8 ? 8 : rowId.length)}',
                            style: const TextStyle(color: _hint, fontSize: 10)),
                        onTap: () => _select(rowId),
                      );
                    },
                  ),
                ),
              ],
            ),
          ),
          const SizedBox(width: 16),
          Expanded(
            child: SingleChildScrollView(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: <Widget>[
                _muted(_message),
                const SizedBox(height: 10),
                if (detail.isEmpty)
                  const _Card(child: Padding(padding: EdgeInsets.all(30), child: Text('尚未选择记忆', style: TextStyle(color: _hint))))
                else ...<Widget>[
                  _Card(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: <Widget>[
                    _title('记忆正文 · $id'),
                    SelectableText(detail['content']?.toString() ?? '', style: const TextStyle(color: Colors.white, fontSize: 13)),
                    const SizedBox(height: 10),
                    _muted(detail['detail_text']?.toString() ?? ''),
                    const SizedBox(height: 8),
                    _muted('类型 ${detail['memory_type'] ?? '—'} · 分层 ${detail['tier'] ?? '—'} · '
                        '置信度 ${detail['confidence'] ?? '—'} · 强度 ${detail['strength'] ?? '—'} · '
                        '双时态 ${detail['valid_from_ms'] ?? '—'} → ${detail['valid_to_ms'] ?? '—'}'),
                    _muted('置信度构成：${jsonEncode(breakdown)} · 陈旧 ${detail['stale'] == true ? '是' : '否'}'),
                  ])),
                  const SizedBox(height: 12),
                  _Card(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: <Widget>[
                    _title('纠正 / 遗忘 / 敏感'),
                    TextField(
                      controller: _correct,
                      style: const TextStyle(color: Colors.white, fontSize: 12),
                      maxLines: 3,
                      decoration: const InputDecoration(hintText: '纠正后的正文…', hintStyle: TextStyle(color: _hint, fontSize: 11)),
                    ),
                    const SizedBox(height: 6),
                    TextField(
                      controller: _reason,
                      style: const TextStyle(color: Colors.white, fontSize: 12),
                      decoration: const InputDecoration(hintText: '理由（写入审计）…', hintStyle: TextStyle(color: _hint, fontSize: 11)),
                    ),
                    const SizedBox(height: 10),
                    Wrap(spacing: 8, runSpacing: 8, children: <Widget>[
                      ElevatedButton(
                        onPressed: _busy || _correct.text.trim().isEmpty
                            ? null
                            : () => _run(widget.client.correctMemory(id, <String, dynamic>{'content': _correct.text.trim()},
                                reason: _reason.text.trim())),
                        child: const Text('纠正这条记忆', style: TextStyle(fontSize: 11)),
                      ),
                      OutlinedButton(
                        onPressed: _busy
                            ? null
                            : () => _run(widget.client.forgetMemory(id, reason: _reason.text.trim(), action: 'forget')),
                        child: const Text('遗忘 / 封存', style: TextStyle(fontSize: 11)),
                      ),
                      OutlinedButton(
                        onPressed: _busy ? null : () => _run(widget.client.setSensitive(id, detail['sensitive'] != true)),
                        child: Text(detail['sensitive'] == true ? '取消敏感标记' : '标记为敏感', style: const TextStyle(fontSize: 11)),
                      ),
                    ]),
                  ])),
                  const SizedBox(height: 12),
                  _Card(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: <Widget>[
                    _title('审计轨迹（${audit.length}）'),
                    if (audit.isEmpty) _muted('暂无审计事件'),
                    for (final item in audit)
                      Padding(
                        padding: const EdgeInsets.only(bottom: 4),
                        child: _muted(jsonEncode(item)),
                      ),
                    _title('关联突触（${related.length}）'),
                    if (related.isEmpty) _muted('暂无关联'),
                    for (final item in related)
                      if (item is Map) _muted('${item['content'] ?? item['id']} · ${item['weight']}'),
                  ])),
                ],
              ]),
            ),
          ),
        ],
      ),
    );
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// 2. 事件时间轴（HLC 全序 + 类型筛选 + 自动刷新）
// ─────────────────────────────────────────────────────────────────────────────
class TimelinePage extends StatefulWidget {
  const TimelinePage({super.key, required this.client});
  final BrainClient client;

  @override
  State<TimelinePage> createState() => _TimelinePageState();
}

class _TimelinePageState extends State<TimelinePage> {
  static const List<String> _types = <String>[
    '全部', 'ingest', 'supersede', 'reinforce', 'tier_change',
    'consolidate', 'policy', 'tombstone', 'recall',
    'overwrite', 'media_interaction',
  ];
  List<Map<String, dynamic>> _rows = <Map<String, dynamic>>[];
  String _type = '全部';
  bool _auto = false;
  Timer? _timer;

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  void _toggleAuto(bool value) {
    setState(() => _auto = value);
    _timer?.cancel();
    if (value) {
      _timer = Timer.periodic(const Duration(seconds: 3), (_) => _load());
    }
  }

  Future<void> _load() async {
    final rows = await widget.client.timeline(
      types: _type == '全部' ? <String>[] : <String>[_type], limit: 300);
    if (mounted) setState(() => _rows = rows);
  }

  String _ts(dynamic value) {
    final ms = value is num ? value.toInt() : int.tryParse(value?.toString() ?? '') ?? 0;
    if (ms <= 0) return '—';
    final time = DateTime.fromMillisecondsSinceEpoch(ms);
    return '${time.year}-${time.month.toString().padLeft(2, '0')}-${time.day.toString().padLeft(2, '0')} '
        '${time.hour.toString().padLeft(2, '0')}:${time.minute.toString().padLeft(2, '0')}';
  }

  String _short(dynamic value) {
    final text = value?.toString() ?? '';
    if (text.isEmpty) return '—';
    return text.length > 8 ? text.substring(0, 8) : text;
  }

  @override
  Widget build(BuildContext context) => PageFrame(
        title: '事件时间轴',
        subtitle: 'journal 全序事件流 + 缓冲层本地事件 + 召回日志（不伪造 journal）',
        child: Column(children: <Widget>[
          Row(children: <Widget>[
            DropdownButton<String>(
              value: _type,
              dropdownColor: const Color(0xFF0A1424),
              style: const TextStyle(color: Colors.white, fontSize: 11),
              items: _types.map((type) => DropdownMenuItem<String>(value: type, child: Text(type))).toList(),
              onChanged: (value) {
                if (value == null) return;
                setState(() => _type = value);
                _load();
              },
            ),
            const SizedBox(width: 16),
            Row(children: <Widget>[
              Switch(value: _auto, onChanged: _toggleAuto),
              const Text('3 秒自动刷新', style: TextStyle(color: _hint, fontSize: 11)),
            ]),
            const SizedBox(width: 16),
            IconButton(icon: const Icon(Icons.refresh, color: _accent, size: 18), onPressed: _load),
            const Spacer(),
            Text('${_rows.length} 条', style: const TextStyle(color: _hint, fontSize: 11)),
          ]),
          const SizedBox(height: 10),
          Expanded(
            child: ListView.builder(
              itemCount: _rows.length,
              itemBuilder: (context, index) {
                final row = _rows[index];
                return ListTile(
                  dense: true,
                  leading: Icon(
                    row['type'] == 'tombstone' || row['type'] == 'node_failed'
                        ? Icons.delete_outline
                        : Icons.circle_outlined,
                    color: row['type'] == 'supersede' ? Colors.orangeAccent : _accent, size: 14),
                  title: Text(row['summary']?.toString() ?? '',
                      maxLines: 2, overflow: TextOverflow.ellipsis,
                      style: const TextStyle(color: Colors.white, fontSize: 12)),
                  subtitle: Text('${row['type'] ?? ''} · ${_ts(row['ts_ms'])} · '
                      '${_short(row['memory_id'])} ${row['reason']?.toString() ?? ''}',
                      style: const TextStyle(color: _hint, fontSize: 10)),
                );
              },
            ),
          ),
        ]),
      );
}

// ─────────────────────────────────────────────────────────────────────────────
// 3. 批量导入（txt/md/json/ndjson/jsonl/csv，走完整写入流）
// ─────────────────────────────────────────────────────────────────────────────
class ImportPage extends StatefulWidget {
  const ImportPage({super.key, required this.client});
  final BrainClient client;

  @override
  State<ImportPage> createState() => _ImportPageState();
}

class _ImportPageState extends State<ImportPage> {
  final _path = TextEditingController();
  Map<String, dynamic> _result = <String, dynamic>{};
  bool _fast = true;
  bool _busy = false;

  @override
  void dispose() {
    _path.dispose();
    super.dispose();
  }

  Future<void> _import() async {
    final path = _path.text.trim().replaceAll('/', Platform.pathSeparator).replaceAll('\\', Platform.pathSeparator);
    final file = File(path);
    if (!file.existsSync()) {
      setState(() => _result = <String, dynamic>{'message': '文件不存在：$path'});
      return;
    }
    setState(() => _busy = true);
    try {
      final text = await file.readAsString();
      final parts = path.split(Platform.pathSeparator).last.split('.');
      final suffix = parts.length > 1 ? '.${parts.last.toLowerCase()}' : '.txt';
      final result = await widget.client.importMemories(text, suffix, fast: _fast);
      if (mounted) setState(() => _result = result);
    } catch (error) {
      if (mounted) setState(() => _result = <String, dynamic>{'message': '读取/导入失败：$error'});
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => PageFrame(
        title: '批量导入',
        subtitle: 'txt 每行一条 / md 空行分段 / json 数组 / ndjson·jsonl 每行一个对象 / csv 含 content 列',
        child: SingleChildScrollView(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: <Widget>[
            _Card(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: <Widget>[
              _title('选择记忆文件'),
              TextField(
                controller: _path,
                style: const TextStyle(color: Colors.white, fontSize: 12),
                decoration: const InputDecoration(
                  hintText: r'例如 C:\Users\me\memories.ndjson',
                  hintStyle: TextStyle(color: _hint, fontSize: 11),
                ),
                onSubmitted: (_) => _busy ? null : _import(),
              ),
              const SizedBox(height: 10),
              Row(children: <Widget>[
                Switch(value: _fast, onChanged: _busy ? null : (value) => setState(() => _fast = value)),
                const Text('快速模式（跳过 LLM 抽取与语义合并，仍保留去重/六引擎/事件日志）',
                    style: TextStyle(color: _hint, fontSize: 11)),
              ]),
              const SizedBox(height: 10),
              ElevatedButton(
                onPressed: _busy ? null : _import,
                child: Text(_busy ? '导入中…' : '开始导入', style: const TextStyle(fontSize: 11)),
              ),
            ])),
            const SizedBox(height: 12),
            _Card(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: <Widget>[
              _title('导入结果'),
              if (_result.isEmpty)
                _muted('尚未导入')
              else ...<Widget>[
                _muted(_result['message']?.toString() ?? _result['error']?.toString() ?? ''),
                const SizedBox(height: 8),
                _muted('解析 ${_result['parsed'] ?? 0} · 写入 ${_result['applied'] ?? 0} · '
                    '重复跳过 ${_result['skipped'] ?? 0} · 失败 ${_result['failed'] ?? 0}'),
                for (final item in _result['errors'] as List<dynamic>? ?? <dynamic>[]) _muted('$item'),
              ],
            ])),
          ]),
        ),
      );
}

// ─────────────────────────────────────────────────────────────────────────────
// 4. 全量配置（后端 BrainConfig 动态表单 + 预设 + 重启内核 + 后端进程设置）
// ─────────────────────────────────────────────────────────────────────────────
class FullConfigPage extends StatefulWidget {
  const FullConfigPage({super.key, required this.client});
  final BrainClient client;

  @override
  State<FullConfigPage> createState() => _FullConfigPageState();
}

class _FullConfigPageState extends State<FullConfigPage> {
  Map<String, dynamic> _sections = <String, dynamic>{};
  final Map<String, Map<String, dynamic>> _draft = <String, Map<String, dynamic>>{};
  final Map<String, TextEditingController> _controllers = <String, TextEditingController>{};
  final Map<String, TextEditingController> _proc = <String, TextEditingController>{};
  final Map<String, TextEditingController> _coreCtl = <String, TextEditingController>{};
  Map<String, dynamic> _mcp = <String, dynamic>{};
  List<String> _presets = <String>[];
  String _preset = 'balanced';
  String _message = '修改任意参数后点「保存配置」；storage/model/merge 需重启内核生效';
  bool _autoStart = true;
  bool _showAdvanced = false;
  int _configTab = 0;

  /// 星云外观唯一真源：flutter_prefs.json 的 nebula 段。
  /// 设置页已不再放第二套滑块，星云页的悬浮开关也写这里。
  Map<String, dynamic> _neb() {
    final value = Prefs.load()['nebula'];
    return value is Map ? Map<String, dynamic>.from(value) : <String, dynamic>{};
  }

  void _setNeb(String key, Object value) {
    final next = _neb()..[key] = value;
    Prefs.save(<String, dynamic>{'nebula': next});
    setState(() => _message = '星云外观已保存（切到星云页即生效）');
  }

  double _nebNum(String key, double fallback) => (_neb()[key] as num?)?.toDouble() ?? fallback;
  bool _nebBool(String key, bool fallback) => _neb()[key] as bool? ?? fallback;

  /// MCP 配置复制：顶部快捷入口与底部说明共用同一份逻辑。
  Future<void> _copyMcp() async {
    final url = _mcp['mcp_url']?.toString() ?? 'http://127.0.0.1:8766/mcp';
    final token = _localToken();
    final payload = jsonEncode(<String, dynamic>{
      'mcpServers': <String, dynamic>{
        'super-brain-memory': <String, dynamic>{
          'type': 'streamable-http', 'url': url,
          'headers': <String, dynamic>{
            'X-Super-Brain-Bridge': token.isEmpty ? '<桥接令牌>' : token,
          },
        },
      },
    });
    await Clipboard.setData(ClipboardData(text: payload));
    if (mounted) {
      setState(() => _message = token.isEmpty
          ? '已复制 JSON；后端未在线，请把 <桥接令牌> 换成 mcp_token.txt 里的值'
          : '已复制完整可用 MCP JSON（含本机令牌 + X-Super-Brain-Bridge 头），任意 Agent 可直接粘贴');
    }
  }


  Widget _cfgTabButton(int index, String label, IconData icon) => ChoiceChip(
        label: Row(mainAxisSize: MainAxisSize.min, children: <Widget>[
          Icon(icon, size: 13,
              color: _configTab == index ? const Color(0xFF06121F) : _accent),
          const SizedBox(width: 5),
          Text(label,
              style: TextStyle(
                fontSize: 11,
                color: _configTab == index
                    ? const Color(0xFF06121F)
                    : const Color(0xFFBFD8F5),
              )),
        ]),
        selected: _configTab == index,
        selectedColor: const Color(0xFF4FC3F7),
        backgroundColor: const Color(0xFF0E1B2D),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(6)),
        onSelected: (_) => setState(() => _configTab = index),
      );

  Widget _nebSlider(String label, String key, double fallback, double min, double max) => SizedBox(
        width: 300,
        child: Row(children: <Widget>[
          SizedBox(width: 78, child: Text(label, style: const TextStyle(color: _hint, fontSize: 11))),
          Expanded(
            child: Slider(
              value: _nebNum(key, fallback).clamp(min, max).toDouble(),
              min: min,
              max: max,
              onChanged: (value) => _setNeb(key, value),
            ),
          ),
          SizedBox(
            width: 52,
            child: Text(
              _nebNum(key, fallback) >= 1
                  ? _nebNum(key, fallback).toStringAsFixed(0)
                  : _nebNum(key, fallback).toStringAsFixed(5),
              style: const TextStyle(color: _accent, fontSize: 10),
            ),
          ),
        ]),
      );

  Widget _nebToggle(String label, String key, bool fallback) => SizedBox(
        width: 150,
        child: Row(mainAxisSize: MainAxisSize.min, children: <Widget>[
          Switch(
            value: _nebBool(key, fallback),
            onChanged: (value) => _setNeb(key, value),
            materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
          ),
          Text(label, style: const TextStyle(color: _hint, fontSize: 11)),
        ]),
      );

  /// 令牌只从本机文件读取，绝不经过 RPC / 网络响应。
  String _localToken() {
    try {
      final base = Platform.environment['APPDATA'];
      if (base == null) return '';
      return File('$base${Platform.pathSeparator}super-brain${Platform.pathSeparator}mcp_token.txt')
          .readAsStringSync()
          .trim();
    } catch (_) {
      return '';
    }
  }

  String _maskedToken(String token) => token.isEmpty
      ? '未找到（请检查 mcp_token.txt）'
      : '${token.substring(0, token.length > 6 ? 6 : token.length)}…（本机读取，不经 RPC）';

  @override
  void initState() {
    super.initState();
    _autoStart = Prefs.get<bool>('auto_start_backend', true);
    for (final key in <String>['backend_command', 'backend_dir', 'backend_args']) {
      _proc[key] = TextEditingController(text: Prefs.get<String>(key, ''));
    }
    _load();
  }

  @override
  void dispose() {
    for (final controller in _controllers.values) {
      controller.dispose();
    }
    for (final controller in _proc.values) {
      controller.dispose();
    }
    for (final controller in _coreCtl.values) {
      controller.dispose();
    }
    super.dispose();
  }

  TextEditingController _ctl(String key, String initial) =>
      _controllers.putIfAbsent(key, () => TextEditingController(text: initial));

  Future<void> _load() async {
    final config = await widget.client.config();
    final presets = await widget.client.configPresets();
    final core = await widget.client.coreMemory();
    final mcp = await widget.client.gatewayStatus();
    if (!mounted) return;
    setState(() {
      _sections = config['sections'] as Map<String, dynamic>? ?? <String, dynamic>{};
      _presets = (presets['names'] as List<dynamic>? ?? <dynamic>[])
          .map((item) => item.toString()).toList();
      if (_presets.isNotEmpty && !_presets.contains(_preset)) _preset = _presets.first;
      _mcp = mcp;
      core.forEach((key, value) {
        _coreCtl.putIfAbsent(key, () => TextEditingController(text: value.toString()));
      });
      _draft.clear();
      for (final controller in _controllers.values) {
        controller.dispose();
      }
      _controllers.clear();
    });
  }

  void _put(String section, String field, dynamic value) {
    _draft.putIfAbsent(section, () => <String, dynamic>{})[field] = value;
  }

  Future<void> _save() async {
    final result = await widget.client.setConfig(_draft);
    if (!mounted) return;
    setState(() => _message = result['message']?.toString() ?? result['error']?.toString() ?? '已保存');
    await _load();
  }

  Future<void> _restart() async {
    final result = await widget.client.restartKernel();
    if (mounted) setState(() => _message = result['message']?.toString() ?? result['error']?.toString() ?? '');
  }

  void _saveProc() {
    Prefs.save(<String, dynamic>{
      'auto_start_backend': _autoStart,
      'backend_command': _proc['backend_command']!.text.trim(),
      'backend_dir': _proc['backend_dir']!.text.trim(),
      'backend_args': _proc['backend_args']!.text.trim(),
    });
    setState(() => _message = '后端进程启动设置已保存（下次启动生效）');
  }

  @override
  Widget build(BuildContext context) {
    final cards = <String, Widget>{};
    _sections.forEach((sectionName, rawSection) {
      if (rawSection is! Map<String, dynamic>) return;
      final fields = <Widget>[];
      rawSection.forEach((fieldName, value) {
        final key = '$sectionName.$fieldName';
        final tip = configTooltip(sectionName, fieldName);
        if (value is bool) {
          fields.add(SwitchListTile(
            dense: true,
            value: (_draft[sectionName]?[fieldName] as bool?) ?? value,
            title: configFieldLabel(sectionName, fieldName, color: Colors.white),
            subtitle: fieldHint(fieldName).isEmpty
                ? null
                : Tooltip(
                    message: tip,
                    child: Text(fieldHint(fieldName), maxLines: 1, overflow: TextOverflow.ellipsis,
                        style: const TextStyle(color: Color(0xFF6D88A8), fontSize: 10))),
            onChanged: (next) {
              _put(sectionName, fieldName.toString(), next);
              setState(() {});
            },
          ));
        } else if (value is num || value is String) {
          final controller = _ctl(key, value.toString());
          fields.add(Padding(
            padding: const EdgeInsets.symmetric(vertical: 3),
            child: Row(children: <Widget>[
              SizedBox(width: 190, child: configFieldLabel(sectionName, fieldName)),
              Expanded(
                child: Tooltip(
                  message: tip,
                  waitDuration: const Duration(milliseconds: 250),
                  child: TextField(
                    controller: controller,
                    style: const TextStyle(color: Colors.white, fontSize: 11),
                    decoration: const InputDecoration(isDense: true, border: OutlineInputBorder()),
                    onChanged: (text) {
                      if (value is int) {
                        _put(sectionName, fieldName.toString(), int.tryParse(text) ?? value);
                      } else if (value is double) {
                        _put(sectionName, fieldName.toString(), double.tryParse(text) ?? value);
                      } else {
                        _put(sectionName, fieldName.toString(), text);
                      }
                    },
                  ),
                ),
              ),
            ]),
          ));
        } else {
          fields.add(Padding(
            padding: const EdgeInsets.symmetric(vertical: 3),
            child: Tooltip(
              message: tip,
              child: _muted('${fieldLabel(fieldName)} = ${jsonEncode(value)}（复杂结构，只读）'),
            ),
          ));
        }
      });
      cards[sectionName] = Padding(
        padding: const EdgeInsets.only(bottom: 12),
        child: _Card(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: <Widget>[
          configSectionTitle(sectionName),
          ...fields,
        ])),
      );
    });
    // 常用分组固定置顶展开；其余收进「高级参数」默认折叠。
    const commonOrder = <String>['gateway', 'activation', 'ui', 'model', 'recall'];
    final commonCards = <Widget>[
      for (final key in commonOrder)
        if (cards.containsKey(key)) cards[key]!,
    ];
    final advancedCards = <Widget>[
      for (final entry in cards.entries)
        if (!commonOrder.contains(entry.key)) entry.value,
    ];

    return PageFrame(
      title: '全量配置',
      subtitle: '后端 BrainConfig 全参数（差异补丁存 %APPDATA%/super-brain/config.json）· 共 ${_sections.length} 组 · 鼠标悬停任意参数名可查看它管什么',
      child: SingleChildScrollView(
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: <Widget>[
          _Card(child: Padding(
            padding: const EdgeInsets.symmetric(vertical: 2),
            child: Row(children: <Widget>[
              _cfgTabButton(0, '接入与启动', Icons.link),
              const SizedBox(width: 8),
              _cfgTabButton(1, '外观与常用', Icons.tune),
              const SizedBox(width: 8),
              _cfgTabButton(2, '高级参数', Icons.expand_more),
              const Spacer(),
              const Text('三类子标签 · 常用置顶 · 高级默认折叠',
                  style: TextStyle(color: _hint, fontSize: 10)),
            ]),
          )),
          const SizedBox(height: 12),
          // ── 第一屏：MCP 接入（不用再翻到页面底部找）──────────────────
          if (_configTab == 0) _Card(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: <Widget>[
            _title('MCP 接入 · 一键复制给任意 Agent'),
            _muted('Streamable HTTP：${_mcp['mcp_url'] ?? 'http://127.0.0.1:8766/mcp'}'),
            const Text('鉴权头必须恰好是 X-Super-Brain-Bridge: <令牌>，不能加 Bearer 前缀',
                style: TextStyle(color: Color(0xFF7FE9FF), fontSize: 11)),
            SelectableText('本机令牌：${_maskedToken(_localToken())}',
                style: const TextStyle(color: _hint, fontSize: 11)),
            const SizedBox(height: 8),
            Wrap(spacing: 10, crossAxisAlignment: WrapCrossAlignment.center, children: <Widget>[
              FilledButton.icon(
                onPressed: _copyMcp,
                icon: const Icon(Icons.content_copy, size: 14),
                label: const Text('复制 MCP 配置（含真实令牌）', style: TextStyle(fontSize: 11)),
              ),
              _muted('粘贴到 Agent 的 mcp.json 即可；完整握手说明见页面底部'),
            ]),
          ])),
          if (_configTab == 0) const SizedBox(height: 12),
          // ── 第一屏：星云外观（唯一入口，与星云页开关同源）────────────
          if (_configTab == 1) _Card(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: <Widget>[
            _title('常用设置 · 星云外观（唯一入口 · 立即生效）'),
            _muted('与星云页左上角开关写同一份配置；「设置」页不再重复放一套。'),
            const SizedBox(height: 6),
            Wrap(spacing: 12, runSpacing: 2, children: <Widget>[
              _nebSlider('焦距', 'focal_length', 900, 400, 1600),
              _nebSlider('星点大小', 'star_size', 3.2, 1, 8),
              _nebSlider('自转速度', 'spin_speed', 0.00035, 0.00005, 0.002),
            ]),
            Wrap(spacing: 2, children: <Widget>[
              _nebToggle('辉光', 'bloom', true),
              _nebToggle('自动旋转', 'auto_rotate', true),
              _nebToggle('突触连线', 'show_edges', false),
              _nebToggle('边缘黑洞', 'show_black_hole', true),
              _nebToggle('中央中枢', 'show_core', true),
              _nebToggle('呼吸闪烁', 'twinkle', true),
            ]),
          ])),
          if (_configTab == 1) const SizedBox(height: 12),
          if (_configTab == 0) _Card(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: <Widget>[
            _title('预设与内核'),
            Wrap(spacing: 10, crossAxisAlignment: WrapCrossAlignment.center, children: <Widget>[
              if (_presets.isNotEmpty)
                DropdownButton<String>(
                  value: _presets.contains(_preset) ? _preset : null,
                  dropdownColor: const Color(0xFF0A1424),
                  style: const TextStyle(color: Colors.white, fontSize: 11),
                  items: _presets
                      .map((name) => DropdownMenuItem<String>(value: name, child: Text(presetLabel(name))))
                      .toList(),
                  onChanged: (value) => setState(() => _preset = value ?? _preset),
                )
              else
                const Text('预设加载中…', style: TextStyle(color: _hint, fontSize: 11)),
              ElevatedButton(
                onPressed: () async {
                  final result = await widget.client.applyPreset(_preset);
                  if (!mounted) return;
                  setState(() => _message = result['message']?.toString() ?? '已应用');
                  await _load();
                },
                child: const Text('应用预设', style: TextStyle(fontSize: 11)),
              ),
              OutlinedButton(onPressed: _restart, child: const Text('应用并重启内核', style: TextStyle(fontSize: 11))),
              OutlinedButton(
                onPressed: () async {
                  final result = await widget.client.resetConfig();
                  if (!mounted) return;
                  setState(() => _message = result['message']?.toString() ?? '已重置');
                  await _load();
                },
                child: const Text('恢复出厂默认', style: TextStyle(fontSize: 11)),
              ),
              ElevatedButton(onPressed: _save, child: const Text('保存配置', style: TextStyle(fontSize: 11))),
            ]),
            const SizedBox(height: 8),
            _muted(_message),
          ])),
          if (_configTab == 0) const SizedBox(height: 12),
          if (_configTab == 1) _title('常用配置（网关 / 星云激活 / 外观 / 模型 / 召回）'),
          if (_configTab == 1) ...commonCards,
          if (_configTab == 2) const SizedBox(height: 4),
          if (_configTab == 2) _Card(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: <Widget>[
            InkWell(
              onTap: () => setState(() => _showAdvanced = !_showAdvanced),
              child: Row(children: <Widget>[
                Icon(_showAdvanced ? Icons.expand_more : Icons.chevron_right,
                    color: _accent, size: 18),
                const SizedBox(width: 6),
                Text('高级参数（${advancedCards.length} 组 · 存储 / 合并 / 遗忘 / 分层 / 丘脑 / 情感 / 同步）',
                    style: const TextStyle(color: Colors.white, fontSize: 13, fontWeight: FontWeight.w600)),
                const Spacer(),
                Text(_showAdvanced ? '点击收起' : '点击展开',
                    style: const TextStyle(color: _hint, fontSize: 10)),
              ]),
            ),
            if (_showAdvanced) ...advancedCards,
          ])),
          if (_configTab == 2) const SizedBox(height: 12),
          if (_configTab == 2) _Card(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: <Widget>[
            _title('Core Memory（可编辑核心记忆块）'),
            for (final entry in _coreCtl.entries)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 3),
                child: TextField(
                  controller: entry.value,
                  maxLines: 2,
                  style: const TextStyle(color: Colors.white, fontSize: 11),
                  decoration: InputDecoration(
                    labelText: entry.key,
                    labelStyle: const TextStyle(color: _hint, fontSize: 10),
                    isDense: true,
                    border: const OutlineInputBorder(),
                  ),
                ),
              ),
            const SizedBox(height: 8),
            OutlinedButton(
              onPressed: () async {
                final blocks = _coreCtl.map<String, dynamic>(
                    (key, value) => MapEntry<String, dynamic>(key, value.text));
                final result = await widget.client.saveCoreMemory(blocks);
                if (mounted) {
                  setState(() => _message = result['message']?.toString() ?? 'Core Memory 已保存');
                }
              },
              child: const Text('保存 Core Memory', style: TextStyle(fontSize: 11)),
            ),
          ])),
          if (_configTab == 0) const SizedBox(height: 12),
          if (_configTab == 0) _Card(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: <Widget>[
            _title('MCP 接入 · 完整说明与握手顺序'),
            _muted('Streamable HTTP：${_mcp['mcp_url'] ?? 'http://127.0.0.1:8766/mcp'}'),
            const Text('鉴权头必须恰好是 X-Super-Brain-Bridge: <令牌>，不能加 Bearer 前缀',
                style: TextStyle(color: Color(0xFF7FE9FF), fontSize: 11)),
            SelectableText('本机令牌：${_maskedToken(_localToken())}',
                style: const TextStyle(color: _hint, fontSize: 11)),
            _muted('令牌位置：${_mcp['token_path'] ?? '%APPDATA%\\super-brain\\mcp_token.txt'}（轮换后需同步更新）'),
            _muted('握手顺序：initialize → notifications/initialized → 带 Mcp-Session-Id 调 tools/call'),
            SelectableText('stdio 启动器：${_mcp['stdio_launcher'] ?? '—'}',
                style: const TextStyle(color: _hint, fontSize: 11)),
            const SizedBox(height: 8),
            OutlinedButton(
              onPressed: _copyMcp,
              child: const Text('复制 MCP 配置（与顶部同一入口）', style: TextStyle(fontSize: 11)),
            ),
          ])),
          if (_configTab == 0) const SizedBox(height: 12),
          if (_configTab == 0) _Card(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: <Widget>[
            _title('后端进程（进程分离）'),
            SwitchListTile(
              dense: true,
              value: _autoStart,
              title: const Text('启动时自动拉起后端', style: TextStyle(color: Colors.white, fontSize: 11)),
              onChanged: (value) => setState(() => _autoStart = value),
            ),
            for (final entry in <Map<String, String>>[
              <String, String>{'key': 'backend_command', 'label': '后端命令（留空优先用安装包内 backend\\headless-server.exe）'},
              <String, String>{'key': 'backend_dir', 'label': '工作目录（留空自动定位项目根 / 安装目录）'},
              <String, String>{'key': 'backend_args', 'label': '启动参数（留空自动推断；exe 模式必须留空）'},
            ])
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 3),
                child: TextField(
                  controller: _proc[entry['key']],
                  style: const TextStyle(color: Colors.white, fontSize: 11),
                  decoration: InputDecoration(
                    labelText: entry['label'],
                    labelStyle: const TextStyle(color: _hint, fontSize: 10),
                    isDense: true,
                    border: const OutlineInputBorder(),
                  ),
                ),
              ),
            const SizedBox(height: 8),
            OutlinedButton(onPressed: _saveProc, child: const Text('保存进程设置', style: TextStyle(fontSize: 11))),
            const SizedBox(height: 6),
            ValueListenableBuilder<String>(
              valueListenable: backendStatus,
              builder: (context, value, _) => _muted('状态：$value'),
            ),
          ])),
        ]),
      ),
    );
  }
}
