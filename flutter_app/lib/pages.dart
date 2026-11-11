import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';

import 'brain_client.dart';
import 'prefs.dart';
import 'win32.dart';

const Color _accent = Color(0xFF4FC3F7);
const Color _hint = Color(0xFF8FA6C4);

/// 通用页面骨架：标题 + 说明 + 内容。
class PageFrame extends StatelessWidget {
  const PageFrame({super.key, required this.title, required this.subtitle, required this.child});
  final String title;
  final String subtitle;
  final Widget child;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.all(22),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Text(title, style: const TextStyle(fontSize: 22, fontWeight: FontWeight.w600, color: Colors.white)),
            const SizedBox(height: 6),
            Text(subtitle, style: const TextStyle(fontSize: 12, color: _hint)),
            const SizedBox(height: 18),
            Expanded(child: child),
          ],
        ),
      );
}

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

class _RefineStat extends StatelessWidget {
  const _RefineStat({required this.label, required this.value, required this.color});
  final String label;
  final int value;
  final Color color;
  @override
  Widget build(BuildContext context) => Expanded(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Text('$value', style: TextStyle(color: color, fontSize: 18, fontWeight: FontWeight.w700)),
            const SizedBox(height: 2),
            Text(label, style: const TextStyle(color: _hint, fontSize: 10)),
          ],
        ),
      );
}

/// 记忆管理：列表 + 写入（走 /v1/events 受理，最终结果由 WS 点亮星云）。
class MemoriesPage extends StatefulWidget {
  const MemoriesPage({super.key, required this.client});
  final BrainClient client;

  @override
  State<MemoriesPage> createState() => _MemoriesPageState();
}

class _MemoriesPageState extends State<MemoriesPage> {
  final TextEditingController _input = TextEditingController();
  List<MemoryRow> _rows = <MemoryRow>[];
  StreamSubscription<Map<String, dynamic>>? _eventsSub;
  String _status = '—';
  String _typeFilter = '全部';
  bool _newestFirst = true;
  Map<String, dynamic>? _refine;
  Timer? _refineTimer;
  bool _refining = false;

  @override
  void initState() {
    super.initState();
    _reload();
    _startRefinePoll();
    _eventsSub = widget.client.events.listen((Map<String, dynamic> event) {
      if (event['type'] == 'node_created' && mounted) _reload();
    });
  }

  @override
  void dispose() {
    _eventsSub?.cancel();
    _refineTimer?.cancel();
    _input.dispose();
    super.dispose();
  }

  Future<void> _reload({bool full = false}) async {
    try {
      final rows = await widget.client.memories(full: full);
      if (mounted) {
        setState(() {
          _rows = rows;
          _status = '共 ${rows.length} 条 · ${_newestFirst ? "最新在前" : "最早在前"}';
        });
      }
    } catch (error) {
      if (mounted) setState(() => _status = '读取失败：$error');
    }
  }

  Future<void> _save() async {
    final content = _input.text.trim();
    if (content.isEmpty) return;
    try {
      final result = await widget.client.saveMemory(content);
      _input.clear();
      setState(() => _status = '已受理 ${result['event_id']}（${result['ack_ms']} ms）→ 后台处理中');
      await _reload();
    } catch (error) {
      setState(() => _status = '写入失败：$error');
    }
  }

  Future<void> _loadRefine() async {
    try {
      final data = await widget.client.call('refine_stats');
      if (mounted) setState(() => _refine = data is Map<String, dynamic> ? data : null);
    } catch (_) {
      // 离线或非 dev 后端无此方法时静默忽略，不影响记忆页其余功能
    }
  }

  void _startRefinePoll() {
    _loadRefine();
    _refineTimer = Timer.periodic(const Duration(seconds: 3), (_) => _loadRefine());
  }

  Future<void> _triggerRefine() async {
    if (_refining) return;
    setState(() => _refining = true);
    try {
      final res = await widget.client.call('refine_trigger', <String, dynamic>{'limit': 50});
      await _loadRefine();
      if (mounted) {
        setState(() => _status = res is Map
            ? '已触发提炼：处理 ${res['processed'] ?? 0} 条 · 剩余 ${res['pending_after'] ?? '?'}'
            : '已触发提炼');
      }
    } catch (e) {
      if (mounted) setState(() => _status = '触发失败：$e');
    } finally {
      if (mounted) setState(() => _refining = false);
    }
  }

  Widget _buildRefineCard() {
    final r = _refine!;
    final pending = (r['pending'] as int?) ?? 0;
    final processing = (r['processing'] as int?) ?? 0;
    final done = (r['done'] as int?) ?? 0;
    final failed = (r['failed'] as int?) ?? 0;
    final total = (r['total'] as int?) ?? 0;
    final pct = ((r['refined_pct'] as num?) ?? 0).toDouble();
    final worker = (r['worker'] as Map<String, dynamic>?) ?? <String, dynamic>{};
    final workerMode = (r['worker_mode']?.toString()) ?? '—';
    return _Card(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Row(
            children: <Widget>[
              const Text('记忆提炼进度',
                  style: TextStyle(color: Colors.white, fontSize: 13, fontWeight: FontWeight.w600)),
              const Spacer(),
              const Text('后端模型提炼 · 每 3s 刷新', style: TextStyle(color: _hint, fontSize: 10)),
            ],
          ),
          const SizedBox(height: 10),
          LinearProgressIndicator(
            value: total > 0 ? pct / 100 : 0,
            backgroundColor: const Color(0xFF16243A),
            valueColor: AlwaysStoppedAnimation<Color>(_accent),
            minHeight: 6,
          ),
          const SizedBox(height: 10),
          Row(
            children: <Widget>[
              _RefineStat(label: '待提炼', value: pending, color: const Color(0xFFFFB74D)),
              _RefineStat(label: '提炼中', value: processing, color: const Color(0xFF4FC3F7)),
              _RefineStat(label: '已完成', value: done, color: const Color(0xFF66BB6A)),
              _RefineStat(label: '失败', value: failed, color: const Color(0xFFFF4D7D)),
            ],
          ),
          const SizedBox(height: 8),
          Text(
            '队列总量 $total · 已提炼 $pct% · Worker($workerMode) 已处理 ${worker['processed'] ?? 0} / 失败 ${worker['failed'] ?? 0}',
            style: const TextStyle(color: _hint, fontSize: 10),
          ),
          const SizedBox(height: 10),
          Row(
            children: <Widget>[
              FilledButton.icon(
                onPressed: _refining ? null : _triggerRefine,
                icon: _refining
                    ? const SizedBox(width: 12, height: 12, child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white))
                    : const Icon(Icons.play_arrow, size: 14),
                label: Text(_refining ? '提炼中…' : '手动开始提炼', style: const TextStyle(fontSize: 11)),
              ),
              const SizedBox(width: 10),
              OutlinedButton.icon(
                onPressed: _refining ? null : _loadRefine,
                icon: const Icon(Icons.refresh, size: 14),
                label: Text('重新拉取进度', style: const TextStyle(fontSize: 11)),
              ),
            ],
          ),
        ],
      ),
    );
  }

  /// 记忆 id 是 UUIDv7：前 48 bit 就是毫秒时间戳，可直接拿来排序/显示，
  /// 不必改后端接口。非 v7 的 id 落在合理年份之外，回退到字典序。
  int? _timeOf(String id) {
    final hex = id.replaceAll('-', '');
    if (hex.length < 12) return null;
    final ms = int.tryParse(hex.substring(0, 12), radix: 16);
    if (ms == null || ms < 946684800000 || ms > 4102444800000) return null;
    return ms;
  }

  String _timeLabel(String id) {
    final ms = _timeOf(id);
    if (ms == null) return '';
    final d = DateTime.fromMillisecondsSinceEpoch(ms).toLocal();
    String two(int v) => v.toString().padLeft(2, '0');
    return '${two(d.month)}-${two(d.day)} ${two(d.hour)}:${two(d.minute)}';
  }

  List<MemoryRow> get _visible {
    final list = _rows
        .where((MemoryRow r) => _typeFilter == '全部' || r.memoryType == _typeFilter)
        .toList();
    list.sort((MemoryRow a, MemoryRow b) {
      final ta = a.updatedAtMs != 0 ? a.updatedAtMs : (_timeOf(a.id) ?? 0);
      final tb = b.updatedAtMs != 0 ? b.updatedAtMs : (_timeOf(b.id) ?? 0);
      final byTime = ta != tb ? ta.compareTo(tb) : a.id.compareTo(b.id);
      return _newestFirst ? -byTime : byTime;
    });
    return list;
  }

  Widget _typeChips() {
    final types = <String>{'全部', for (final MemoryRow r in _rows) r.memoryType}.toList();
    return Wrap(
      spacing: 6,
      runSpacing: 4,
      children: <Widget>[
        for (final type in types)
          ChoiceChip(
            label: Text(
              type == '全部'
                  ? '全部 ${_rows.length}'
                  : '$type ${_rows.where((MemoryRow r) => r.memoryType == type).length}',
              style: TextStyle(
                fontSize: 10,
                color: _typeFilter == type
                    ? const Color(0xFF06121F)
                    : const Color(0xFFBFD8F5),
              ),
            ),
            selected: _typeFilter == type,
            selectedColor: const Color(0xFF4FC3F7),
            backgroundColor: const Color(0xFF0E1B2D),
            shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(6)),
            onSelected: (_) => setState(() => _typeFilter = type),
          ),
      ],
    );
  }

  @override
  Widget build(BuildContext context) {
    final visible = _visible;
    return PageFrame(
      title: '记忆管理',
      subtitle: '按类型分类 · 默认最新记忆在最顶部 · 写入走丘脑路由 → 语义合并 → 六引擎投影',
      child: Scrollbar(
        thumbVisibility: true,
        thickness: 8,
        radius: const Radius.circular(8),
        child: SingleChildScrollView(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: <Widget>[
          _Card(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                Row(
                  children: <Widget>[
                    Expanded(
                      child: TextField(
                        controller: _input,
                        style: const TextStyle(color: Colors.white, fontSize: 13),
                        decoration: const InputDecoration(
                          hintText: '输入一条新记忆…',
                          hintStyle: TextStyle(color: Color(0xFF63788F), fontSize: 13),
                          border: InputBorder.none,
                        ),
                        onSubmitted: (_) => _save(),
                      ),
                    ),
                    const SizedBox(width: 12),
                    FilledButton(onPressed: _save, child: const Text('写入记忆')),
                    const SizedBox(width: 12),
                    OutlinedButton(onPressed: () => _reload(full: true), child: const Text('刷新')),
                  ],
                ),
                const SizedBox(height: 10),
                Row(
                  crossAxisAlignment: CrossAxisAlignment.center,
                  children: <Widget>[
                    Expanded(child: _typeChips()),
                    const SizedBox(width: 8),
                    OutlinedButton.icon(
                      onPressed: () => setState(() {
                        _newestFirst = !_newestFirst;
                        _status = '共 ${_rows.length} 条 · ${_newestFirst ? "最新在前" : "最早在前"}';
                      }),
                      icon: Icon(_newestFirst ? Icons.arrow_downward : Icons.arrow_upward, size: 13),
                      label: Text(_newestFirst ? '最新在前' : '最早在前',
                          style: const TextStyle(fontSize: 10)),
                    ),
                  ],
                ),
              ],
            ),
          ),
          const SizedBox(height: 10),
          if (_refine != null) _buildRefineCard(),
          if (_refine != null) const SizedBox(height: 10),
          Align(
              alignment: Alignment.centerLeft,
              child: Text('$_status · 当前显示 ${visible.length} 条',
                  style: const TextStyle(color: _hint, fontSize: 11))),
          const SizedBox(height: 10),
          _Card(
            child: ListView.separated(
              shrinkWrap: true,
              physics: const NeverScrollableScrollPhysics(),
              itemCount: visible.length,
              separatorBuilder: (_, __) =>
                  const Divider(color: Color(0xFF16243A), height: 1),
              itemBuilder: (context, index) {
                final row = visible[index];
                final when = _timeLabel(row.id);
                return ListTile(
                  leading: Icon(
                    row.memoryType == 'emotion' ? Icons.favorite : Icons.memory,
                    color: row.memoryType == 'emotion'
                        ? const Color(0xFFFF4D7D)
                        : _accent,
                    size: 18,
                  ),
                  title: Text(row.content,
                      style: const TextStyle(color: Colors.white, fontSize: 13)),
                  subtitle: Text(
                    '${when.isEmpty ? "" : "$when · "}${row.memoryType} · ${row.tier} · '
                    'PAD ${row.pad.map((v) => v.toStringAsFixed(1)).join("/")}',
                    style: const TextStyle(color: _hint, fontSize: 10),
                  ),
                );
              },
            ),
          ),
            ],
          ),
        ),
      ),
    );
  }
}

/// 召回测试：结果分数 + 命中通道，可在星云页搜索触发光脉冲。
class RecallPage extends StatefulWidget {
  const RecallPage({super.key, required this.client});
  final BrainClient client;

  @override
  State<RecallPage> createState() => _RecallPageState();
}

class _RecallPageState extends State<RecallPage> {
  final TextEditingController _query = TextEditingController();
  List<RecallHit> _hits = <RecallHit>[];
  String _status = '输入查询后回车';
  bool _deep = false;
  bool _busy = false;

  Future<void> _run() async {
    if (_query.text.trim().isEmpty) {
      setState(() => _status = '请输入查询内容');
      return;
    }
    // 关键：点下去立刻给反馈，否则慢召回时界面像「没反应」
    setState(() {
      _busy = true;
      _status = _deep ? '深度召回中（六通道精排，可能较慢）…' : '检索中…';
    });
    try {
      final hits = await widget.client.recall(_query.text, deep: _deep);
      if (!mounted) return;
      setState(() {
        _hits = hits;
        _busy = false;
        _status = hits.isEmpty
            ? '未命中（换个关键词，或打开「深度召回」）'
            : '命中 ${hits.length} 条 · ${_deep ? '六通道精排' : '字符 2-gram 只读投影'}';
      });
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _busy = false;
        _status = '召回失败：$error';
      });
    }
  }

  @override
  Widget build(BuildContext context) => PageFrame(
        title: '召回测试',
        subtitle: '在线受理路径不含检索计算；这里直接查询只读投影，星云页搜索会用同样结果发射光脉冲',
        child: Column(
          children: <Widget>[
            _Card(
              child: Row(
                children: <Widget>[
                  Expanded(
                    child: TextField(
                      controller: _query,
                      style: const TextStyle(color: Colors.white, fontSize: 13),
                      decoration: const InputDecoration(hintText: '例如：发版冲突', hintStyle: TextStyle(color: Color(0xFF63788F)), border: InputBorder.none),
                      onSubmitted: (_) => _run(),
                    ),
                  ),
                  FilledButton(onPressed: _busy ? null : _run, child: const Text('召回')),
                ],
              ),
            ),
            const SizedBox(height: 8),
            Row(
              children: <Widget>[
                const Text('深度召回（六通道精排，可能慢）', style: TextStyle(color: _hint, fontSize: 11)),
                SizedBox(
                  height: 28,
                  child: Switch(value: _deep, onChanged: (bool v) => setState(() => _deep = v)),
                ),
                if (_busy)
                  const SizedBox(width: 14, height: 14, child: CircularProgressIndicator(strokeWidth: 2)),
              ],
            ),
            Align(alignment: Alignment.centerLeft, child: Text(_status, style: const TextStyle(color: _hint, fontSize: 11))),
            const SizedBox(height: 10),
            Expanded(
              child: _Card(
                child: ListView.builder(
                  itemCount: _hits.length,
                  itemBuilder: (context, index) {
                    final hit = _hits[index];
                    return ListTile(
                      title: Text(hit.content, style: const TextStyle(color: Colors.white, fontSize: 13)),
                      trailing: Text(hit.score.toStringAsFixed(3), style: const TextStyle(color: _accent, fontSize: 12)),
                      subtitle: Text('${hit.memoryType} · fts', style: const TextStyle(color: _hint, fontSize: 10)),
                    );
                  },
                ),
              ),
            ),
          ],
        ),
      );
}

/// 接口与端口：显示后端地址、健康状态、curl 示例。
class InterfacePage extends StatefulWidget {
  const InterfacePage({super.key, required this.client});
  final BrainClient client;

  @override
  State<InterfacePage> createState() => _InterfacePageState();
}

class _InterfacePageState extends State<InterfacePage> {
  Map<String, dynamic> _health = <String, dynamic>{};
  String _error = '';

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final health = await widget.client.health();
      if (mounted) setState(() => _health = health);
    } catch (error) {
      if (mounted) setState(() => _error = '$error');
    }
  }

  @override
  Widget build(BuildContext context) {
    final base = widget.client.base;
    return PageFrame(
      title: '接口与端口',
      subtitle: 'Flutter 只是外壳；所有数据来自 v3 Daemon（HTTP + WebSocket）',
      child: ListView(
        children: <Widget>[
          _Card(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                Text('HTTP  $base', style: const TextStyle(color: _accent, fontSize: 13)),
                Text('WS    ${widget.client.wsUrl}', style: const TextStyle(color: _accent, fontSize: 13)),
                const SizedBox(height: 10),
                Text(_error.isEmpty ? '内核状态：${_health['ok'] == true ? '正常' : '未知'}' : '连接失败：$_error',
                    style: const TextStyle(color: _hint, fontSize: 12)),
                if (_health.isNotEmpty)
                  Text('inbox ${_health['inbox']} · worker ${_health['worker']} · journal ${_health['journal_events']}',
                      style: const TextStyle(color: _hint, fontSize: 11)),
              ],
            ),
          ),
          const SizedBox(height: 12),
          _Card(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                const Text('写入一条记忆（curl）', style: TextStyle(color: Colors.white, fontSize: 13)),
                const SizedBox(height: 8),
                SelectableText('curl -X POST $base/v1/events -H "Content-Type: application/json" -d "{\"content\":\"测试记忆\",\"agent_id\":\"flutter\"}"',
                    style: const TextStyle(color: Color(0xFF7FE9FF), fontSize: 11)),
                const SizedBox(height: 14),
                const Text('MCP / HTTP 工具：save_memory · recall_memory · timeline_query · profile_read · conflict_report · health_check',
                    style: TextStyle(color: _hint, fontSize: 11)),
              ],
            ),
          ),
          const SizedBox(height: 12),
          OutlinedButton(onPressed: _load, child: const Text('重新检测')),
        ],
      ),
    );
  }
}

/// 设置：后端地址/端口 + 3D 星云参数（同步到后端 nebula_prefs.json，跨设备一致）。
class SettingsPage extends StatefulWidget {
  const SettingsPage({super.key, required this.client, required this.onReconnect});
  final BrainClient client;
  final void Function(String host, int httpPort, int wsPort) onReconnect;

  @override
  State<SettingsPage> createState() => _SettingsPageState();
}

class _SettingsPageState extends State<SettingsPage> {
  late final TextEditingController _host = TextEditingController(text: Prefs.get<String>('host', '127.0.0.1'));
  late final TextEditingController _http = TextEditingController(text: Prefs.get<int>('http_port', 8768).toString());
  late final TextEditingController _ws = TextEditingController(text: Prefs.get<int>('ws_port', 8767).toString());
  String _status = '—';
  int _settingsTab = 0;
  bool _fullRender = Prefs.get<bool>('full_render', false);

  Widget _settingsChip(int index, String label, IconData icon) => ChoiceChip(
        label: Row(mainAxisSize: MainAxisSize.min, children: <Widget>[
          Icon(icon, size: 13,
              color: _settingsTab == index ? const Color(0xFF06121F) : _accent),
          const SizedBox(width: 5),
          Text(label,
              style: TextStyle(
                fontSize: 11,
                color: _settingsTab == index
                    ? const Color(0xFF06121F)
                    : const Color(0xFFBFD8F5),
              )),
        ]),
        selected: _settingsTab == index,
        selectedColor: const Color(0xFF4FC3F7),
        backgroundColor: const Color(0xFF0E1B2D),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(6)),
        onSelected: (_) => setState(() => _settingsTab = index),
      );

  String _nebSummary() {
    final value = Prefs.load()['nebula'];
    if (value is! Map || value.isEmpty) return '当前使用出厂默认值（尚未调整过）';
    return value.entries.map((e) => '${e.key} = ${e.value}').join('  ·  ');
  }

  void _setFullRender(bool value) {
    Prefs.save(<String, dynamic>{'full_render': value});
    setState(() {
      _fullRender = value;
      _status = value
          ? '已开启全量渲染：切到「星云视图」按 500 条/批渐进加载全部记忆'
          : '已关闭全量渲染：恢复默认 200 节点视图';
    });
  }

  void _save() {
    final host = _host.text.trim().isEmpty ? '127.0.0.1' : _host.text.trim();
    final httpPort = int.tryParse(_http.text) ?? 8768;
    final wsPort = int.tryParse(_ws.text) ?? 8767;
    Prefs.save(<String, dynamic>{'host': host, 'http_port': httpPort, 'ws_port': wsPort});
    widget.onReconnect(host, httpPort, wsPort);
    setState(() => _status = '连接参数已保存并重新连接');
  }

  @override
  Widget build(BuildContext context) => PageFrame(
        title: '设置',
        subtitle: '连接参数写在 %APPDATA%/super-brain/flutter_prefs.json；3D 参数同时同步到后端，多端一致',
        child: ListView(
          children: <Widget>[
            _Card(
              child: Row(children: <Widget>[
                _settingsChip(0, '连接', Icons.settings_ethernet),
                const SizedBox(width: 8),
                _settingsChip(1, '外观', Icons.blur_on),
                const SizedBox(width: 8),
                _settingsChip(2, '诊断', Icons.medical_services_outlined),
                const Spacer(),
                const Text('设置页分类子标签', style: TextStyle(color: _hint, fontSize: 10)),
              ]),
            ),
            const SizedBox(height: 12),
            if (_settingsTab == 0)
              _Card(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: <Widget>[
                    const Text('后端连接', style: TextStyle(color: Colors.white, fontSize: 14)),
                    const SizedBox(height: 12),
                    Row(
                      children: <Widget>[
                        Expanded(flex: 3, child: _field('主机', _host)),
                        const SizedBox(width: 12),
                        Expanded(child: _field('HTTP 端口', _http)),
                        const SizedBox(width: 12),
                        Expanded(child: _field('WS 端口', _ws)),
                      ],
                    ),
                    const SizedBox(height: 12),
                    FilledButton(onPressed: _save, child: const Text('保存并重新连接')),
                  ],
                ),
              ),
            if (_settingsTab == 1)
              _Card(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: <Widget>[
                    const Text('星云外观（唯一入口）', style: TextStyle(color: Colors.white, fontSize: 14)),
                    const SizedBox(height: 6),
                    const Text(
                      '焦距 / 星点大小 / 自转 / 辉光 / 连线 / 黑洞 / 中枢等外观参数，'
                      '统一在「系统设置 → 全量配置 → 外观与常用」修改并立即生效。'
                      '这里不再重复放一套滑块，星云页左上角的悬浮开关写的是同一份配置。',
                      style: TextStyle(color: _hint, fontSize: 11, height: 1.5),
                    ),
                    const SizedBox(height: 8),
                    Text(_nebSummary(), style: const TextStyle(color: _accent, fontSize: 10, height: 1.5)),
                    const Divider(color: Color(0xFF22344C), height: 22),
                    SwitchListTile(
                      dense: true,
                      contentPadding: EdgeInsets.zero,
                      value: _fullRender,
                      onChanged: (value) => _setFullRender(value),
                      title: const Text('[●] 全量渲染模式（性能警告）',
                          style: TextStyle(color: Colors.white, fontSize: 12)),
                      subtitle: const Text(
                        '加载全部记忆节点（默认仅 200 条）。3000+ 节点时后端按 500 条/批推送、'
                        '前端边收边渲染，并自动启用 LOD：镜头拉远即关闭连线/标签/光晕。'
                        'GTX 1060 建议保持「突触连线」关闭。',
                        style: TextStyle(color: _hint, fontSize: 10, height: 1.5),
                      ),
                    ),
                  ],
                ),
              ),
            if (_settingsTab == 2)
              _Card(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: <Widget>[
                    const Text('启动与进程诊断', style: TextStyle(color: Colors.white, fontSize: 14)),
                    const SizedBox(height: 6),
                    const Text(
                      '''· 双击没反应：多半是上次启动异常留下的「无窗口僵尸前端」占着单实例端口 38767。
· 新版会自愈：第二实例发现端口被占却找不到主窗口时，自动强杀僵尸并接管。
· 启动异常写入 %APPDATA%/super-brain/startup_error.log，并释放端口锁后退出。
· 后端是独立进程；托盘「完全退出」会先优雅停后端再关前端，不会留孤儿。''',
                      style: TextStyle(color: _hint, fontSize: 11, height: 1.7),
                    ),
                    const SizedBox(height: 10),
                    SwitchListTile(
                      dense: true,
                      value: Prefs.get<bool>('hide_on_close', false),
                      title: const Text('关窗隐藏到托盘',
                          style: TextStyle(color: Colors.white, fontSize: 11)),
                      subtitle: const Text(
                        '仅在任务栏右下角确实能看到托盘图标时开启；'
                        '否则窗口被隐藏后会找不到（默认关闭，点 X 直接退出）',
                        style: TextStyle(color: _hint, fontSize: 10),
                      ),
                      onChanged: (bool value) {
                        Prefs.save(<String, dynamic>{'hide_on_close': value});
                        Win32Window.setTrayMode(value);
                        setState(() => _status = value
                            ? '已开启关窗隐藏到托盘'
                            : '已关闭隐藏：点 X 直接退出，窗口不会再消失');
                      },
                    ),
                    const SizedBox(height: 8),
                    OutlinedButton(
                      onPressed: () async {
                        try {
                          final s = await widget.client.snapshot();
                          final engines = (s['engine_health']?['engines'] as List<dynamic>? ?? <dynamic>[]);
                          if (mounted) setState(() => _status = '后端在线 · ${engines.length} 个引擎已探活');
                        } catch (error) {
                          if (mounted) setState(() => _status = '后端未连接：$error');
                        }
                      },
                      child: const Text('检查后端状态', style: TextStyle(fontSize: 11)),
                    ),
                  ],
                ),
              ),
            const SizedBox(height: 10),
            Text(_status, style: const TextStyle(color: _hint, fontSize: 11)),
          ],
        ),
      );

  Widget _field(String label, TextEditingController controller) => TextField(
        controller: controller,
        style: const TextStyle(color: Colors.white, fontSize: 12),
        decoration: InputDecoration(labelText: label, labelStyle: const TextStyle(color: _hint, fontSize: 11), border: const OutlineInputBorder()),
      );

}

/// 系统概览：六引擎实时明细 + 投影/空间/召回真相 + 生命循环任务。
/// 数据全部来自后端 `storage.overview` RPC（后端自有存储层，不新建连接、不伪造数值）。
class SystemPage extends StatefulWidget {
  const SystemPage({super.key, required this.client});
  final BrainClient client;

  @override
  State<SystemPage> createState() => _SystemPageState();
}

class _SystemPageState extends State<SystemPage> {
  List<Map<String, dynamic>> _tasks = <Map<String, dynamic>>[];
  Map<String, dynamic> _snapshot = <String, dynamic>{};
  Map<String, dynamic> _ov = <String, dynamic>{};
  String _status = '—';

  @override
  void initState() {
    super.initState();
    _reload();
  }

  Future<void> _reload() async {
    final tasks = await widget.client.tasks();
    final snapshot = await widget.client.snapshot();
    Map<String, dynamic> ov;
    try {
      ov = await widget.client.storageOverview();
    } catch (_) {
      ov = <String, dynamic>{};
    }
    if (!mounted) return;
    setState(() {
      _tasks = tasks;
      _snapshot = snapshot;
      _ov = ov;
    });
  }

  Future<void> _run(String name) async {
    final result = await widget.client.runTask(name);
    if (!mounted) return;
    setState(() => _status = '${result['ok'] == true ? '执行成功' : '执行失败'}：$name · ${result['message'] ?? ''}');
    _reload();
  }

  Map<String, dynamic> _m(String key) {
    final value = _ov[key];
    return value is Map ? Map<String, dynamic>.from(value) : <String, dynamic>{};
  }

  List<dynamic> _l(String key) {
    final value = _ov[key];
    return value is List ? value : <dynamic>[];
  }

  String _join(String group, String key) {
    final value = _m(group)[key];
    return value is List ? value.map((e) => '$e').join(' → ') : '—';
  }

  String _fmt(dynamic value) {
    if (value is Map) {
      return value.entries.map((e) => '${e.key}:${e.value}').join('   ');
    }
    if (value is List) return value.map((e) => '$e').join('、');
    return value == null ? '—' : '$value';
  }

  Widget _kpi(String num, String label) => Expanded(
        child: _Card(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              Text(num, style: const TextStyle(color: _accent, fontSize: 19, fontWeight: FontWeight.w600)),
              const SizedBox(height: 4),
              Text(label, style: const TextStyle(color: _hint, fontSize: 10)),
            ],
          ),
        ),
      );

  Widget _title(String text) => Padding(
        padding: const EdgeInsets.only(bottom: 10),
        child: Text(text, style: const TextStyle(color: Colors.white, fontSize: 13)),
      );

  Widget _line(String key, String value) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 3),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            SizedBox(width: 104, child: Text(key, style: const TextStyle(color: _accent, fontSize: 11))),
            Expanded(child: Text(value, style: const TextStyle(color: _hint, fontSize: 11))),
          ],
        ),
      );

  Widget _engineCard() {
    final rows = <Widget>[_title('六引擎实时明细 · 职责 / 写入策略 / 实际落盘')];
    final engines = _l('engines');
    if (engines.isEmpty) {
      rows.add(const Text('后端未升级 storage.overview，或内核为进程分离模式',
          style: TextStyle(color: _hint, fontSize: 11)));
    }
    for (final raw in engines) {
      final e = Map<String, dynamic>.from(raw as Map);
      rows.add(Padding(
        padding: const EdgeInsets.symmetric(vertical: 7),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Text('${e['name']}  ·  ${e['ok'] == false ? '异常' : '正常'}  ·  ${e['rows']} 行  ·  ${e['latency_ms']}ms',
                style: const TextStyle(color: Colors.white, fontSize: 11)),
            const SizedBox(height: 2),
            Text('${e['role']}', style: const TextStyle(color: _accent, fontSize: 11)),
            Text('写入：${e['write_policy']}', style: const TextStyle(color: _hint, fontSize: 10)),
            Text('存什么：${e['stores']}', style: const TextStyle(color: _hint, fontSize: 10)),
            Text('实时：${e['counts_text']}', style: const TextStyle(color: _hint, fontSize: 10)),
          ],
        ),
      ));
    }
    final totals = _m('totals');
    rows.add(const SizedBox(height: 6));
    rows.add(_line('主库总量', '全量 ${_fmt(totals['neurons_all'])} · 活跃 ${_fmt(totals['active'])} · 封存 ${_fmt(totals['archived'])}'));
    rows.add(_line('待重放', '${_snapshot['sync_status']?['pending_events'] ?? _fmt(totals['outbox_pending'])}'));
    rows.add(_line('分层 / 类型', '${_fmt(totals['by_tier'])}  /  ${_fmt(totals['by_type'])}'));
    return _Card(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: rows));
  }

  Widget _taskCard() => _Card(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            _title('生命循环任务'),
            ..._tasks.map((task) => ListTile(
                  dense: true,
                  title: Text('${task['name']}', style: const TextStyle(color: Colors.white, fontSize: 12)),
                  subtitle: Text('${task['at']} · ${task['description'] ?? ''} · 上次 ${task['last_run'] ?? '—'}',
                      style: const TextStyle(color: _hint, fontSize: 10)),
                  trailing: TextButton(
                    onPressed: () => _run('${task['name']}'),
                    child: const Text('执行'),
                  ),
                )),
            const SizedBox(height: 8),
            Text(_status, style: const TextStyle(color: _hint, fontSize: 11)),
          ],
        ),
      );

  Widget _truthCard() {
    final projection = _m('projection');
    final spaces = _m('spaces_binding');
    final recall = _m('recall');
    final totals = _m('totals');
    final cleanup = _m('cleanup');
    return _Card(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          _title('存储投影 / 空间绑定 / 召回链路真相'),
          _line('投影模式', '${projection['mode'] ?? '—'}（事实源 ${projection['source_of_truth'] ?? '—'}）'),
          _line('每条必写', _join('projection', 'every_write')),
          _line('条件写入', _join('projection', 'conditional')),
          _line('不落盘', _join('projection', 'no_disk')),
          _line('路由说明', '${projection['note'] ?? '—'}'),
          const Divider(color: Color(0xFF1B2A42), height: 18),
          _line('记忆空间', '${_fmt(spaces['spaces_total'])} 个（启用 ${_fmt(spaces['spaces_active'])}）· 已绑定记忆 ${_fmt(spaces['bound_memories'])} 条'),
          _line('空间清单', _fmt(spaces['names'])),
          _line('namespace', _fmt(spaces['namespace_distribution'])),
          _line('分类真相', '${spaces['truth'] ?? '—'}'),
          const Divider(color: Color(0xFF1B2A42), height: 18),
          _line('快速召回', '${recall['fast'] ?? '—'}'),
          _line('深度召回', '${recall['deep'] ?? '—'}'),
          _line('向量真相', '${recall['vector_truth'] ?? '—'}'),
          _line('关联真相', '${recall['graph_truth'] ?? '—'}'),
          const Divider(color: Color(0xFF1B2A42), height: 18),
          _line('认知质量', '零 PAD ${_fmt(totals['zero_pad_active'])} 条 · conf=0.45 ${_fmt(totals['confidence_045_active'])} 条（0.6B 降级期产物，非脏数据）'),
          _line('关系表', 'synapses ${_fmt(totals['synapses'])} · hyperedges ${_fmt(totals['hyperedges'])}'),
          _line('清洗快照', '${cleanup['latest_snapshot'] ?? '—'}'),
          _line('清洗动作', '${cleanup['action'] ?? '—'}'),
          _line('清洗结果', '${cleanup['result'] ?? '—'}'),
          _line('未动数据', _fmt(cleanup['untouched'])),
          const SizedBox(height: 8),
          Text('生成于 ${_fmt(_ov['generated_at_ms'])} · ${_fmt(_ov['mode'])} 模式 · 实测数据 ${_fmt(recall['measured_at'])}',
              style: const TextStyle(color: _hint, fontSize: 10)),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final totals = _m('totals');
    final spaces = _m('spaces_binding');
    return PageFrame(
      title: '系统概览',
      subtitle: '六引擎实时计数 · 投影足迹 · 认知质量 · 空间绑定 · 召回通道（全部经 WebSocket RPC）',
      child: SingleChildScrollView(
        child: Column(
          children: <Widget>[
            Row(
              children: <Widget>[
                _kpi(_fmt(totals['active']), '活跃记忆（真实计数）'),
                const SizedBox(width: 10),
                _kpi(_fmt(totals['zero_pad_active']), '零 PAD（认知缺失）'),
                const SizedBox(width: 10),
                _kpi('${_fmt(spaces['spaces_total'])} / ${_fmt(spaces['bound_memories'])}', '空间 / 已绑定记忆'),
                const SizedBox(width: 10),
                _kpi('${_l('engines').length}', '引擎（5 必写 + 条件投影）'),
                const SizedBox(width: 10),
                SizedBox(
                  width: 76,
                  child: OutlinedButton(onPressed: _reload, child: const Text('刷新')),
                ),
              ],
            ),
            const SizedBox(height: 14),
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                Expanded(child: _engineCard()),
                const SizedBox(width: 14),
                Expanded(child: _taskCard()),
              ],
            ),
            const SizedBox(height: 14),
            _truthCard(),
          ],
        ),
      ),
    );
  }
}

/// 展示数据：全量快照 JSON + 导出。
class DataPage extends StatefulWidget {
  const DataPage({super.key, required this.client});
  final BrainClient client;

  @override
  State<DataPage> createState() => _DataPageState();
}

class _DataPageState extends State<DataPage> {
  String _json = '{}';
  String _status = '—';

  @override
  void initState() {
    super.initState();
    _reload();
  }

  Future<void> _reload() async {
    final snapshot = await widget.client.snapshot();
    if (!mounted) return;
    setState(() => _json = const JsonEncoder.withIndent('  ').convert(snapshot));
  }

  Future<void> _export() async {
    final result = await widget.client.exportAll();
    setState(() => _status = result['error']?.toString() ??
        '已导出：${result['dashboard']} / ${result['memories']}');
  }

  @override
  Widget build(BuildContext context) => PageFrame(
        title: '展示数据',
        subtitle: '六面板原始快照（JSON）与记忆 NDJSON 导出',
        child: Column(
          children: <Widget>[
            Row(
              children: <Widget>[
                FilledButton(onPressed: _export, child: const Text('导出快照 + 记忆')),
                const SizedBox(width: 12),
                OutlinedButton(onPressed: _reload, child: const Text('重新读取')),
              ],
            ),
            const SizedBox(height: 8),
            Align(alignment: Alignment.centerLeft, child: Text(_status, style: const TextStyle(color: _hint, fontSize: 11))),
            const SizedBox(height: 10),
            Expanded(
              child: _Card(
                child: SingleChildScrollView(
                  child: SelectableText(_json, style: const TextStyle(color: Color(0xFF7FE9FF), fontSize: 11, height: 1.5)),
                ),
              ),
            ),
          ],
        ),
      );
}

/// 模型配置：双通道本地/云端、模型状态、参数保存与内核热重启。
class ModelPage extends StatefulWidget {
  const ModelPage({super.key, required this.client});
  final BrainClient client;

  @override
  State<ModelPage> createState() => _ModelPageState();
}

class _ModelPageState extends State<ModelPage> {
  Map<String, dynamic> _status = <String, dynamic>{};
  Map<String, dynamic> _sections = <String, dynamic>{};
  late final TextEditingController _embedUrl = TextEditingController();
  late final TextEditingController _llmUrl = TextEditingController();
  String _message = '—';

  @override
  void initState() {
    super.initState();
    _reload();
  }

  Future<void> _reload() async {
    final status = await widget.client.modelStatus();
    final config = await widget.client.config();
    if (!mounted) return;
    final sections = config['sections'] as Map<String, dynamic>? ?? <String, dynamic>{};
    final model = sections['model'] as Map<String, dynamic>? ?? <String, dynamic>{};
    setState(() {
      _status = status;
      _sections = sections;
      _embedUrl.text = model['embed_base_url']?.toString() ?? '';
      _llmUrl.text = model['llm_base_url']?.toString() ?? '';
    });
  }

  Future<void> _save() async {
    final result = await widget.client.setConfig(<String, dynamic>{
      'model': <String, dynamic>{'embed_base_url': _embedUrl.text.trim(), 'llm_base_url': _llmUrl.text.trim()},
    });
    setState(() => _message = result['message']?.toString() ?? result['error']?.toString() ?? '已保存');
  }

  Future<void> _restart() async {
    final result = await widget.client.restartKernel();
    setState(() => _message = result['message']?.toString() ?? result['error']?.toString() ?? '已重启');
    _reload();
  }

  @override
  Widget build(BuildContext context) => PageFrame(
        title: '模型配置',
        subtitle: 'Embedding / LLM 两条通道各自独立供应商（地址、模型名、密钥分开），保存后重启内核生效',
        child: ListView(
          children: <Widget>[
            _Card(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: <Widget>[
                  const Text('当前实际使用', style: TextStyle(color: Colors.white, fontSize: 13)),
                  const SizedBox(height: 8),
                  Text('Embedding：${_status['embedding_backend'] ?? '—'} · ${_status['embedder']?['kind'] ?? '—'}',
                      style: const TextStyle(color: _accent, fontSize: 11)),
                  Text('LLM：${_status['llm_backend'] ?? '—'} · ${_status['llm']?['kind'] ?? '—'}',
                      style: const TextStyle(color: _accent, fontSize: 11)),
                  Text('CUDA ${_status['use_cuda']} · 维度 ${_status['dense_dim']}',
                      style: const TextStyle(color: _hint, fontSize: 11)),
                ],
              ),
            ),
            const SizedBox(height: 12),
            _Card(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: <Widget>[
                  const Text('两条独立云端通道', style: TextStyle(color: Colors.white, fontSize: 13)),
                  const SizedBox(height: 12),
                  TextField(controller: _embedUrl, style: const TextStyle(color: Colors.white, fontSize: 12),
                      decoration: const InputDecoration(labelText: 'Embedding API 地址', labelStyle: TextStyle(color: _hint, fontSize: 11), border: OutlineInputBorder())),
                  const SizedBox(height: 10),
                  TextField(controller: _llmUrl, style: const TextStyle(color: Colors.white, fontSize: 12),
                      decoration: const InputDecoration(labelText: 'LLM API 地址', labelStyle: TextStyle(color: _hint, fontSize: 11), border: OutlineInputBorder())),
                  const SizedBox(height: 12),
                  Row(children: <Widget>[
                    FilledButton(onPressed: _save, child: const Text('保存')),
                    const SizedBox(width: 12),
                    OutlinedButton(onPressed: _restart, child: const Text('应用并重启内核')),
                  ]),
                  const SizedBox(height: 8),
                  Text(_message, style: const TextStyle(color: _hint, fontSize: 11)),
                ],
              ),
            ),
            const SizedBox(height: 12),
            _Card(
              child: Text('完整参数分组（${_sections.keys.length} 组）：${_sections.keys.join(" / ")}\n'
                  '在「设置」页可逐项微调；这里通过 WS RPC 的 config.get / config.set 读写同一份用户差异补丁。',
                  style: const TextStyle(color: _hint, fontSize: 11, height: 1.6)),
            ),
          ],
        ),
      );
}

/// 同步与备份：立即同步、创建快照、快照列表。
class SyncPage extends StatefulWidget {
  const SyncPage({super.key, required this.client});
  final BrainClient client;

  @override
  State<SyncPage> createState() => _SyncPageState();
}

class _SyncPageState extends State<SyncPage> {
  List<Map<String, dynamic>> _snapshots = <Map<String, dynamic>>[];
  String _status = '—';
  final TextEditingController _repoUrl = TextEditingController();
  final TextEditingController _localDir = TextEditingController();
  final TextEditingController _deviceId = TextEditingController();
  final TextEditingController _interval = TextEditingController();

  @override
  void initState() {
    super.initState();
    _reload();
    _loadConfig();
  }

  @override
  void dispose() {
    _repoUrl.dispose();
    _localDir.dispose();
    _deviceId.dispose();
    _interval.dispose();
    super.dispose();
  }

  Future<void> _loadConfig() async {
    try {
      final config = await widget.client.config();
      final sections = config['sections'] as Map<String, dynamic>? ?? <String, dynamic>{};
      final sync = sections['sync'] as Map<String, dynamic>? ?? <String, dynamic>{};
      if (!mounted) return;
      _repoUrl.text = sync['repo_url']?.toString() ?? '';
      _localDir.text = sync['local_repo_dir']?.toString() ?? '';
      _deviceId.text = sync['device_id']?.toString() ?? '';
      _interval.text = sync['sync_interval_sec']?.toString() ?? '300';
      setState(() {});
    } catch (error) {
      if (mounted) setState(() => _status = '读取同步配置失败：$error');
    }
  }

  Future<void> _saveRepo() async {
    final result = await widget.client.setConfig(<String, dynamic>{
      'sync': <String, dynamic>{
        'repo_url': _repoUrl.text.trim(),
        'local_repo_dir': _localDir.text.trim(),
        'device_id': _deviceId.text.trim(),
        'sync_interval_sec': int.tryParse(_interval.text.trim()) ?? 300,
      },
    });
    if (!mounted) return;
    setState(() => _status = result['error']?.toString() ?? '同步仓库已保存；重启内核后生效');
  }

  Future<void> _reload() async {
    try {
      final rows = await widget.client.snapshots();
      if (mounted) setState(() => _snapshots = rows);
    } catch (error) {
      // 后端离线 / 返回格式变化时只影响这一页，绝不能冒泡成全局退出。
      if (mounted) setState(() => _status = '快照列表暂不可用：$error');
    }
  }

  Future<void> _sync() async {
    final result = await widget.client.syncOnce();
    setState(() => _status = result['error']?.toString() ??
        'pull ${result['pulled']} · replay ${result['replayed']} · push ${result['pushed']}');
    _reload();
  }

  Future<void> _snapshot() async {
    final result = await widget.client.createSnapshot();
    setState(() => _status = result['error']?.toString() ?? '已创建 ${result['name']}');
    _reload();
  }

  @override
  Widget build(BuildContext context) => PageFrame(
        title: '同步与备份',
        subtitle: '事件日志是唯一真源；快照只是某一刻状态，多端同步按 HLC 因果序幂等重放',
        child: Column(
          children: <Widget>[
            Row(children: <Widget>[
              FilledButton(onPressed: _sync, child: const Text('立即同步')),
              const SizedBox(width: 12),
              OutlinedButton(onPressed: _snapshot, child: const Text('创建快照')),
              const SizedBox(width: 12),
              OutlinedButton(onPressed: _reload, child: const Text('刷新')),
            ]),
            const SizedBox(height: 10),
            _Card(child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                const Text('同步仓库设置（Git 私库 / 本机镜像）',
                    style: TextStyle(color: Colors.white, fontSize: 13)),
                const SizedBox(height: 8),
                TextField(
                  controller: _repoUrl,
                  style: const TextStyle(color: Colors.white, fontSize: 11),
                  decoration: const InputDecoration(
                    labelText: '仓库地址 repo_url（留空表示不同步）',
                    labelStyle: TextStyle(color: _hint, fontSize: 10),
                    isDense: true, border: OutlineInputBorder(),
                  ),
                ),
                const SizedBox(height: 6),
                TextField(
                  controller: _localDir,
                  style: const TextStyle(color: Colors.white, fontSize: 11),
                  decoration: const InputDecoration(
                    labelText: '本地仓库目录 local_repo_dir',
                    labelStyle: TextStyle(color: _hint, fontSize: 10),
                    isDense: true, border: OutlineInputBorder(),
                  ),
                ),
                const SizedBox(height: 6),
                Row(children: <Widget>[
                  Expanded(child: TextField(
                    controller: _deviceId,
                    style: const TextStyle(color: Colors.white, fontSize: 11),
                    decoration: const InputDecoration(
                      labelText: '设备标识 device_id',
                      labelStyle: TextStyle(color: _hint, fontSize: 10),
                      isDense: true, border: OutlineInputBorder(),
                    ),
                  )),
                  const SizedBox(width: 8),
                  SizedBox(width: 130, child: TextField(
                    controller: _interval,
                    style: const TextStyle(color: Colors.white, fontSize: 11),
                    decoration: const InputDecoration(
                      labelText: '同步间隔(秒)',
                      labelStyle: TextStyle(color: _hint, fontSize: 10),
                      isDense: true, border: OutlineInputBorder(),
                    ),
                  )),
                ]),
                const SizedBox(height: 8),
                OutlinedButton(onPressed: _saveRepo, child: const Text('保存仓库设置', style: TextStyle(fontSize: 11))),
              ],
            )),
            const SizedBox(height: 10),
            Align(alignment: Alignment.centerLeft, child: Text(_status, style: const TextStyle(color: _hint, fontSize: 11))),
            const SizedBox(height: 10),
            Expanded(
              child: _Card(
                child: ListView.builder(
                  itemCount: _snapshots.length,
                  itemBuilder: (context, index) {
                    final row = _snapshots[index];
                    return ListTile(
                      leading: const Icon(Icons.backup_outlined, color: _accent, size: 18),
                      title: Text('${row['name']}', style: const TextStyle(color: Colors.white, fontSize: 12)),
                      subtitle: Text('${row['counts']}', style: const TextStyle(color: _hint, fontSize: 10)),
                    );
                  },
                ),
              ),
            ),
          ],
        ),
      );
}

class AboutPage extends StatelessWidget {
  const AboutPage({super.key});

  static const TextStyle _body =
      TextStyle(color: _hint, fontSize: 11, height: 1.7);
  static const TextStyle _head = TextStyle(
      color: Colors.white, fontSize: 13, fontWeight: FontWeight.w600);

  Widget _section(String title, List<String> lines) => _Card(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Text(title, style: _head),
            const SizedBox(height: 8),
            ...lines.map((String line) => Padding(
                  padding: const EdgeInsets.only(bottom: 3),
                  child: Text(line, style: _body),
                )),
          ],
        ),
      );

  @override
  Widget build(BuildContext context) => PageFrame(
        title: '关于 超级大脑',
        subtitle: 'Super Brain v3.1 · Living Memory · 本地优先的长期记忆中枢',
        child: ListView(
          children: <Widget>[
            _section('它是什么', <String>[
              '一套完全跑在本机的长期记忆系统：把事实、偏好、情绪、项目文档与技能分层保存，'
                  '再按需注入给任意 AI Agent，让不同会话、不同工具共享同一份记忆。',
              '数据不出本机，不依赖云端账号；没有「用对话改进模型」这种条款。',
              '前端 Flutter，后端 Python 冻结为 headless-server.exe，两者进程分离，'
                  '前端崩溃不影响记忆服务。',
            ]),
            const SizedBox(height: 10),
            _section('六引擎存储', <String>[
              'SQLite（WAL + FTS5）：神经元主库、双时态、outbox、全文检索',
              'LanceDB：1024 维语义向量（BGE-M3），dense 召回',
              'Kùzu：实体 / 关系 / 超图编码',
              'LMDB：KV 画像与偏好',
              'DuckDB：时序 OLAP 与命中率埋点',
              'HyperNetX：超图与 PAD 认知分区',
              '事件日志（NDJSON journal）是唯一真源：先落盘 fsync → inbox 队列 → 202 accepted，'
                  'Worker 异步做抽取 / 去重 / 语义合并 / PAD / FSRS，再由 WebSocket 广播增量。',
            ]),
            const SizedBox(height: 10),
            _section('分层上下文', <String>[
              '运行时记忆：MEMORY.md（项目约定）与 USER.md（用户画像），每轮确定性注入，'
                  '带 critical / normal / low 优先级，写满自动归档低优先级旧条目。',
              '项目档案：先检索标题与摘要，需要细节才读全文；记录 last_read，最久未用优先归档。',
              '记忆空间：可建多个空间并指定 Provider，只有激活的空间参与召回。',
              '技能管理：版本化技能，按使用成功率自进化，低成功率自动标记待改进。',
              '注入由前额叶收敛：只给 Top 3 摘要 + 「停止发散」执行约束，'
                  '避免 Agent 复述原文或自由联想。',
            ]),
            const SizedBox(height: 10),
            _section('Agent 接入（MCP）', <String>[
              '地址：http://127.0.0.1:8766/mcp（streamable-http）',
              '鉴权头必须恰好是 X-Super-Brain-Bridge: <令牌>，不能加 Bearer 前缀。',
              '握手顺序：initialize → notifications/initialized → 带 Mcp-Session-Id 调 tools/call；'
                  '裸调会挂起。令牌在 %APPDATA%\\super-brain\\mcp_token.txt。',
              '当前暴露 52 个工具 + 6 个 MCP resources。首次接入先调 context_manifest '
                  '拿能力清单（层 / 角色 / actions / routes / 预算）。',
              '「全量配置 → 复制 MCP 配置」会产出含真实令牌的完整 JSON，任意 Agent 可直接粘贴。',
            ]),
            const SizedBox(height: 10),
            _section('交互与桌面能力', <String>[
              '3D 星云：纯 Dart CustomPainter + 旋转矩阵 + 透视投影，零第三方 pub 依赖；'
                  '球壳分布的星系 + 跨簇弱连线，拖拽翻滚、滚轮缩放、双击复位、检索触发星爆。',
              '2D 语义环 / PAD 散点 / 召回归因三块分析面板可点选联动到记忆检查器。',
              '硬件仪表：CPU / 内存 / NVIDIA 显卡利用率、显存与温度，2 秒采样。',
              '托盘与单实例：dart:ffi 调 Win32；点 X 隐藏进托盘，托盘右键「完全退出」'
                  '会先让后端优雅停机再退出，Job Object 保证前端被强杀时后端不残留。',
              '开机自启、密钥串（DPAPI）、数据目录迁移均可在「桌面能力」页配置。',
            ]),
            const SizedBox(height: 10),
            _section('长时挂机与数据安全', <String>[
              '日志：10 MB 单文件切割、保留 5 份，避免挂机把磁盘写满。',
              'WAL：每 5 分钟及停机时执行 PRAGMA wal_checkpoint(TRUNCATE)，收缩 -wal 文件。',
              '子进程：CREATE_NO_WINDOW + Job Object，Worker 不会变成孤儿。',
              '数据目录迁移采用「复制 + 指针」，默认保留原目录；目标已有记忆库时拒绝覆盖。',
            ]),
            const SizedBox(height: 10),
            _section('版本与许可', <String>[
              'Super Brain v3.1 · Flutter 3.47.9 / Dart 3.13 · Python 3.12 · PyInstaller 6.22',
              '后端基于 rusEFI/epicEFI 无关的独立实现；Flutter 为唯一前端，发行包不含任何桌面 UI 依赖。',
              '图标、界面与内核均为本项目自研，本地使用，不上传任何遥测。',
            ]),
          ],
        ),
      );
}

/// 空间内记忆列表：从「记忆空间」页点击空间进入。
/// 样式对齐 MemoriesPage（类型胶囊过滤 + 深色卡片列表），
/// 自带 AppBar 返回按钮（app_shell 是 IndexedStack 自管导航，根路由 push 覆盖全屏）。
class SpaceMemoriesPage extends StatefulWidget {
  const SpaceMemoriesPage({super.key, required this.client, required this.spaceId, required this.spaceName});
  final BrainClient client;
  final String spaceId;
  final String spaceName;

  @override
  State<SpaceMemoriesPage> createState() => _SpaceMemoriesPageState();
}

class _SpaceMemoriesPageState extends State<SpaceMemoriesPage> {
  final TextEditingController _search = TextEditingController();
  List<Map<String, dynamic>> _rows = <Map<String, dynamic>>[];
  String _error = '';
  String _typeFilter = '全部';
  bool _newestFirst = true;
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    _reload();
  }

  Future<void> _reload() async {
    if (mounted) setState(() => _loading = true);
    try {
      final rows = await widget.client.memoryList(
        namespace: 'topic:${widget.spaceId}', limit: 500, keyword: _search.text.trim());
      if (mounted) setState(() { _rows = rows; _error = ''; _loading = false; });
    } catch (error) {
      if (mounted) setState(() { _error = '$error'; _loading = false; });
    }
  }

  String _timeOf(Map<String, dynamic> row) {
    final ms = row['updated_at_ms'] ?? row['valid_from_ms'];
    final n = (ms is num) ? ms.toInt() : int.tryParse('$ms') ?? 0;
    if (n <= 0) return '';
    final d = DateTime.fromMillisecondsSinceEpoch(n).toLocal();
    String two(int v) => v.toString().padLeft(2, '0');
    return '${d.year}-${two(d.month)}-${two(d.day)} ${two(d.hour)}:${two(d.minute)}';
  }

  List<Map<String, dynamic>> get _visible {
    final list = _rows
        .where((r) => _typeFilter == '全部' || '${r['memory_type'] ?? ''}' == _typeFilter)
        .toList();
    int ts(Map<String, dynamic> r) {
      final ms = r['updated_at_ms'] ?? r['valid_from_ms'];
      return (ms is num) ? ms.toInt() : int.tryParse('${ms ?? ''}') ?? 0;
    }
    list.sort((a, b) => _newestFirst ? ts(b).compareTo(ts(a)) : ts(a).compareTo(ts(b)));
    return list;
  }

  Widget _typeChips() {
    final types = <String>{'全部', for (final r in _rows) '${r['memory_type'] ?? '其他'}'}.toList();
    int countOf(String t) =>
        t == '全部' ? _rows.length : _rows.where((r) => '${r['memory_type'] ?? '其他'}' == t).length;
    return Wrap(
      spacing: 6, runSpacing: 4,
      children: <Widget>[
        for (final type in types)
          ChoiceChip(
            label: Text('$type ${countOf(type)}',
                style: TextStyle(fontSize: 10,
                    color: _typeFilter == type ? const Color(0xFF06121F) : const Color(0xFFBFD8F5))),
            selected: _typeFilter == type,
            selectedColor: const Color(0xFF4FC3F7),
            backgroundColor: const Color(0xFF0E1B2D),
            shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(6)),
            onSelected: (_) => setState(() => _typeFilter = type),
          ),
      ],
    );
  }

  @override
  Widget build(BuildContext context) {
    final visible = _visible;
    return Scaffold(
      backgroundColor: const Color(0xFF05080E),
      appBar: AppBar(
        backgroundColor: const Color(0xFF070D18),
        foregroundColor: Colors.white,
        elevation: 0,
        title: Text(widget.spaceName, style: const TextStyle(fontSize: 15)),
      ),
      body: PageFrame(
        title: '${widget.spaceName} · 记忆列表',
        subtitle: 'namespace=topic:${widget.spaceId} · 写入走丘脑路由 → 语义合并 → 六引擎投影',
        child: Column(children: <Widget>[
          _Card(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: <Widget>[
              Row(children: <Widget>[
                Expanded(child: TextField(
                  controller: _search,
                  style: const TextStyle(color: Colors.white, fontSize: 13),
                  decoration: const InputDecoration(
                    hintText: '按关键字过滤本空间记忆…',
                    hintStyle: TextStyle(color: Color(0xFF63788F), fontSize: 13),
                    border: InputBorder.none,
                  ),
                  onSubmitted: (_) => _reload(),
                )),
                const SizedBox(width: 12),
                OutlinedButton(onPressed: _reload, child: const Text('刷新')),
              ]),
              const SizedBox(height: 10),
              Row(crossAxisAlignment: CrossAxisAlignment.center, children: <Widget>[
                Expanded(child: _typeChips()),
                const SizedBox(width: 8),
                OutlinedButton.icon(
                  onPressed: () => setState(() => _newestFirst = !_newestFirst),
                  icon: Icon(_newestFirst ? Icons.arrow_downward : Icons.arrow_upward, size: 13),
                  label: Text(_newestFirst ? '最新在前' : '最早在前', style: const TextStyle(fontSize: 10)),
                ),
              ]),
            ]),
          ),
          const SizedBox(height: 10),
          if (_error.isNotEmpty)
            Align(alignment: Alignment.centerLeft,
                child: Text('读取失败：$_error', style: const TextStyle(color: Colors.redAccent, fontSize: 11)))
          else
            Align(alignment: Alignment.centerLeft,
                child: Text('共 ${_rows.length} 条 · ${_newestFirst ? "最新在前" : "最早在前"} · 当前显示 ${visible.length} 条',
                    style: const TextStyle(color: _hint, fontSize: 11))),
          const SizedBox(height: 10),
          Expanded(child: _loading
              ? const Center(child: CircularProgressIndicator(color: _accent, strokeWidth: 2))
              : _Card(child: visible.isEmpty
                  ? const Center(child: Text('该空间暂无匹配记忆', style: TextStyle(color: _hint, fontSize: 12)))
                  : ListView.separated(
                      itemCount: visible.length,
                      separatorBuilder: (_, __) => const Divider(color: Color(0xFF16243A), height: 1),
                      itemBuilder: (context, index) {
                        final row = visible[index];
                        final type = '${row['memory_type'] ?? ''}';
                        final pad = row['pad'];
                        final padStr = (pad is List && pad.length >= 3)
                            ? 'PAD ${(pad[0] as num?)?.toStringAsFixed(1) ?? '?'}/${(pad[1] as num?)?.toStringAsFixed(1) ?? '?'}/${(pad[2] as num?)?.toStringAsFixed(1) ?? '?'}'
                            : '';
                        final when = _timeOf(row);
                        return ListTile(
                          leading: Icon(
                            type == 'emotion' ? Icons.favorite : Icons.memory,
                            color: type == 'emotion' ? const Color(0xFFFF4D7D) : _accent,
                            size: 18,
                          ),
                          title: Text('${row['content'] ?? ''}',
                              style: const TextStyle(color: Colors.white, fontSize: 13)),
                          subtitle: Padding(padding: const EdgeInsets.only(top: 3), child: Text(
                            [if (when.isNotEmpty) when, if (type.isNotEmpty) type, '${row['tier'] ?? ''}', padStr]
                                .where((s) => s.isNotEmpty).join(' · '),
                            style: const TextStyle(color: _hint, fontSize: 10),
                          )),
                        );
                      },
                    ))),
        ]),
      ),
    );
  }
}
