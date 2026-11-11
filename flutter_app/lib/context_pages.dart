import 'package:flutter/material.dart';

import 'brain_client.dart';
import 'pages.dart';

/// 分层上下文 UI —— 对齐 dsh-mnemon 的「记忆系统」五个页签：
/// 状态 / 运行时记忆 / 项目档案 / 记忆空间 / 技能管理。
///
/// 运行时记忆每轮确定性注入；项目档案先给摘要、按需读全文；记忆空间按激活状态
/// 参与召回；技能带版本与使用反馈。全部走 v3 WebSocket RPC，零第三方依赖。

const Color _hint = Color(0xFF6B839F);
const Color _accent = Color(0xFF4FC3F7);
const Color _panel = Color(0xFF0A1424);
const Color _panel2 = Color(0xFF16233A);

Widget _frame(String title, String subtitle, Widget child) => Padding(
      padding: const EdgeInsets.all(18),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          Text(title, style: const TextStyle(color: Colors.white, fontSize: 20, fontWeight: FontWeight.w600)),
          const SizedBox(height: 4),
          Text(subtitle, style: const TextStyle(color: _hint, fontSize: 11)),
          const SizedBox(height: 16),
          Expanded(child: child),
        ],
      ),
    );

Widget _box(Widget child) => Container(
      width: double.infinity,
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: _panel,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: const Color(0xFF142238)),
      ),
      child: child,
    );

TextField _field(TextEditingController controller, String hint, {int maxLines = 1}) => TextField(
      controller: controller,
      maxLines: maxLines,
      style: const TextStyle(color: Colors.white, fontSize: 12),
      decoration: InputDecoration(
        hintText: hint,
        hintStyle: const TextStyle(color: Color(0xFF63788F), fontSize: 12),
        border: const OutlineInputBorder(),
        isDense: true,
      ),
    );

// ── 1. 状态 ────────────────────────────────────────────────────────────
class ContextStatusPage extends StatefulWidget {
  const ContextStatusPage({super.key, required this.client});
  final BrainClient client;

  @override
  State<ContextStatusPage> createState() => _ContextStatusPageState();
}

class _ContextStatusPageState extends State<ContextStatusPage> {
  Map<String, dynamic> _status = <String, dynamic>{};
  String _error = '';

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final data = await widget.client.contextStatus();
      if (!mounted) return;
      setState(() {
        _status = data;
        _error = data['error']?.toString() ?? '';
      });
    } catch (error) {
      if (mounted) setState(() => _error = '$error');
    }
  }

  @override
  Widget build(BuildContext context) {
    final layers = (_status['layers'] as Map<String, dynamic>? ?? <String, dynamic>{});
    return _frame('上下文状态', 'Source / Strategy / Provider 一览 · 数据全在本地',
      ListView(
        children: <Widget>[
          if (_error.isNotEmpty)
            _box(Text('读取失败：$_error', style: const TextStyle(color: Colors.redAccent, fontSize: 12))),
          _box(Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: <Widget>[
              const Text('数据目录', style: TextStyle(color: _hint, fontSize: 11)),
              const SizedBox(height: 6),
              Text(_status['root']?.toString() ?? '—',
                  style: const TextStyle(color: Colors.white, fontSize: 12)),
              const SizedBox(height: 8),
              Text('revision ${_status['revision'] ?? '—'} · Provider: ${(_status['providers'] as List<dynamic>? ?? <dynamic>[]).join('、')}',
                  style: const TextStyle(color: _hint, fontSize: 11)),
            ],
          )),
          const SizedBox(height: 10),
          for (final entry in layers.entries)
            Padding(
              padding: const EdgeInsets.only(bottom: 10),
              child: _box(Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: <Widget>[
                  Text(entry.key, style: const TextStyle(color: _accent, fontSize: 13, fontWeight: FontWeight.w600)),
                  const SizedBox(height: 8),
                  ...((entry.value as Map<String, dynamic>? ?? <String, dynamic>{})
                      .entries
                      .map((e) => Padding(
                            padding: const EdgeInsets.symmetric(vertical: 2),
                            child: Text('${e.key}: ${e.value}',
                                style: const TextStyle(color: Colors.white, fontSize: 11)),
                          ))),
                ],
              )),
            ),
          Align(
            alignment: Alignment.centerLeft,
            child: TextButton.icon(
              onPressed: _load,
              icon: const Icon(Icons.refresh, size: 16),
              label: const Text('刷新'),
            ),
          ),
        ],
      ),
    );
  }
}

// ── 2. 运行时记忆 ──────────────────────────────────────────────────────
class RuntimeMemoryPage extends StatefulWidget {
  const RuntimeMemoryPage({super.key, required this.client});
  final BrainClient client;

  @override
  State<RuntimeMemoryPage> createState() => _RuntimeMemoryPageState();
}

class _RuntimeMemoryPageState extends State<RuntimeMemoryPage> {
  Map<String, dynamic> _data = <String, dynamic>{};
  String _error = '';

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final data = await widget.client.runtimeGet();
      if (mounted) setState(() => _data = data);
    } catch (error) {
      if (mounted) setState(() => _error = '$error');
    }
  }

  List<dynamic> _bucket(String key) =>
      (_data[key] as List<dynamic>? ?? <dynamic>[]);

  Future<void> _add(String target) async {
    final content = TextEditingController();
    String importance = 'normal';
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, setDialog) => AlertDialog(
          backgroundColor: _panel,
          title: Text('新增 ${target == 'memory' ? 'MEMORY.md' : 'USER.md'} 条目',
              style: const TextStyle(color: Colors.white, fontSize: 15)),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            children: <Widget>[
              _field(content, '内容'),
              const SizedBox(height: 10),
              DropdownButton<String>(
                value: importance,
                dropdownColor: _panel,
                style: const TextStyle(color: Colors.white, fontSize: 12),
                items: const <DropdownMenuItem<String>>[
                  DropdownMenuItem<String>(value: 'critical', child: Text('critical')),
                  DropdownMenuItem<String>(value: 'normal', child: Text('normal')),
                  DropdownMenuItem<String>(value: 'low', child: Text('low')),
                ],
                onChanged: (String? v) => setDialog(() => importance = v ?? 'normal'),
              ),
            ],
          ),
          actions: <Widget>[
            TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('取消')),
            FilledButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('保存')),
          ],
        ),
      ),
    );
    if (ok != true || content.text.trim().isEmpty) return;
    await widget.client.runtimeMutate('add', target,
        content: content.text.trim(), importance: importance);
    await _load();
  }

  Future<void> _remove(String target, String oldText) async {
    await widget.client.runtimeMutate('remove', target, oldText: oldText);
    await _load();
  }

  Widget _section(String target, String label, int limit) {
    final rows = _bucket(target);
    final used = rows.fold<int>(0, (int sum, dynamic row) {
      final map = Map<String, dynamic>.from(row as Map);
      return sum + (map['content']?.toString().length ?? 0);
    });
    return _box(Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        Row(
          children: <Widget>[
            Text('$label  ·  ${rows.length} 条  ·  ~$used / $limit 字符',
                style: const TextStyle(color: _accent, fontSize: 12)),
            const Spacer(),
            IconButton(
              tooltip: '新增',
              icon: const Icon(Icons.add, size: 18, color: _accent),
              onPressed: () => _add(target),
            ),
          ],
        ),
        const Divider(color: Color(0xFF142238)),
        if (rows.isEmpty)
          const Text('（空）', style: TextStyle(color: _hint, fontSize: 11)),
        for (final row in rows)
          Builder(builder: (ctx) {
            final map = Map<String, dynamic>.from(row as Map);
            final text = map['content']?.toString() ?? '';
            return ListTile(
              dense: true,
              leading: Chip(
                label: Text(map['importance']?.toString() ?? 'normal',
                    style: const TextStyle(fontSize: 9)),
                visualDensity: VisualDensity.compact,
              ),
              title: Text(text, style: const TextStyle(color: Colors.white, fontSize: 12)),
              trailing: IconButton(
                icon: const Icon(Icons.delete_outline, size: 16, color: _hint),
                onPressed: () => _remove(target, text),
              ),
            );
          }),
      ],
    ));
  }

  @override
  Widget build(BuildContext context) {
    final limits = (_data['limits'] as Map<String, dynamic>? ?? <String, dynamic>{});
    return _frame('运行时记忆', '每轮确定性注入 · 写满后把低优先级旧条目归档（不删除）',
      ListView(
        children: <Widget>[
          if (_error.isNotEmpty)
            _box(Text('读取失败：$_error', style: const TextStyle(color: Colors.redAccent, fontSize: 12))),
          _section('memory', 'MEMORY.md（项目约定）', (limits['memory'] as int? ?? 8000)),
          const SizedBox(height: 10),
          _section('user', 'USER.md（用户画像）', (limits['user'] as int? ?? 4000)),
          const SizedBox(height: 10),
          _box(Text('已归档 ${_bucket('archived').length} 条 · revision ${_data['revision'] ?? '—'}',
              style: const TextStyle(color: _hint, fontSize: 11))),
        ],
      ),
    );
  }
}

// ── 3. 项目档案 ────────────────────────────────────────────────────────
class DocumentsPage extends StatefulWidget {
  const DocumentsPage({super.key, required this.client});
  final BrainClient client;

  @override
  State<DocumentsPage> createState() => _DocumentsPageState();
}

class _DocumentsPageState extends State<DocumentsPage> {
  final TextEditingController _keyword = TextEditingController();
  List<dynamic> _rows = <dynamic>[];
  String _error = '';

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final rows = await widget.client.documentsList(keyword: _keyword.text.trim());
      if (mounted) setState(() => _rows = rows);
    } catch (error) {
      if (mounted) setState(() => _error = '$error');
    }
  }

  Future<void> _read(String id, String title) async {
    final data = await widget.client.documentsRead(id);
    if (!mounted) return;
    await showDialog<void>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: _panel,
        title: Text(title, style: const TextStyle(color: Colors.white, fontSize: 15)),
        content: SizedBox(
          width: 640,
          child: SingleChildScrollView(
            child: SelectableText(data['content']?.toString() ?? '(空)',
                style: const TextStyle(color: Colors.white, fontSize: 12)),
          ),
        ),
        actions: <Widget>[
          TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('关闭')),
        ],
      ),
    );
    await _load();
  }

  Future<void> _create() async {
    final title = TextEditingController();
    final desc = TextEditingController();
    final content = TextEditingController();
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: _panel,
        title: const Text('新建项目档案', style: TextStyle(color: Colors.white, fontSize: 15)),
        content: SizedBox(
          width: 560,
          child: Column(mainAxisSize: MainAxisSize.min, children: <Widget>[
            _field(title, '标题'),
            const SizedBox(height: 8),
            _field(desc, '摘要（列表里只显示这一行）'),
            const SizedBox(height: 8),
            _field(content, '全文 Markdown', maxLines: 6),
          ]),
        ),
        actions: <Widget>[
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('取消')),
          FilledButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('创建')),
        ],
      ),
    );
    if (ok != true || title.text.trim().isEmpty) return;
    await widget.client.documentsMutate('create',
        title: title.text, description: desc.text, content: content.text);
    await _load();
  }

  @override
  Widget build(BuildContext context) => _frame(
        '项目档案',
        '先检索标题与摘要，再按需读全文 · 记录 last_read，最久未读优先归档',
        Column(children: <Widget>[
          _box(Row(children: <Widget>[
            Expanded(child: _field(_keyword, '搜索标题 / 摘要')),
            const SizedBox(width: 10),
            FilledButton(onPressed: _load, child: const Text('检索')),
            const SizedBox(width: 10),
            FilledButton(onPressed: _create, child: const Text('新建')),
          ])),
          if (_error.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: Text('读取失败：$_error', style: const TextStyle(color: Colors.redAccent, fontSize: 11)),
            ),
          const SizedBox(height: 10),
          Expanded(
            child: _box(ListView.builder(
              itemCount: _rows.length,
              itemBuilder: (ctx, index) {
                final row = Map<String, dynamic>.from(_rows[index] as Map);
                return ListTile(
                  title: Text('${row['title'] ?? ''}  ·  ${row['bytes'] ?? 0} B',
                      style: const TextStyle(color: Colors.white, fontSize: 12)),
                  subtitle: Text('${row['description'] ?? ''}${row['archived'] == true ? '  [已归档]' : ''}',
                      style: const TextStyle(color: _hint, fontSize: 11)),
                  trailing: Row(mainAxisSize: MainAxisSize.min, children: <Widget>[
                    TextButton(
                      onPressed: () => _read('${row['id']}', '${row['title']}'),
                      child: const Text('读全文'),
                    ),
                    IconButton(
                      tooltip: '归档',
                      icon: const Icon(Icons.archive_outlined, size: 16, color: _hint),
                      onPressed: () async {
                        await widget.client.documentsMutate('archive', id: '${row['id']}');
                        await _load();
                      },
                    ),
                  ]),
                );
              },
            )),
          ),
        ]),
      );
}

// ── 4. 记忆空间 ────────────────────────────────────────────────────────
class MemorySpacesPage extends StatefulWidget {
  const MemorySpacesPage({super.key, required this.client});
  final BrainClient client;

  @override
  State<MemorySpacesPage> createState() => _MemorySpacesPageState();
}

class _MemorySpacesPageState extends State<MemorySpacesPage> {
  List<dynamic> _spaces = <dynamic>[];
  Map<String, int> _counts = <String, int>{}; // namespace → 记忆数（topics.list.by_namespace）
  String _error = '';
  String _tip = '';

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final data = await widget.client.spacesList();
      Map<String, int> counts = <String, int>{};
      try {
        final topics = await widget.client.topicsList();
        final raw = topics['by_namespace'];
        if (raw is Map) {
          raw.forEach((k, v) => counts['$k'] = (v is num) ? v.toInt() : 0);
        }
      } catch (_) {} // 计数失败不阻塞空间列表
      if (mounted) {
        setState(() {
          _spaces = data['spaces'] as List<dynamic>? ?? <dynamic>[];
          _counts = counts;
        });
      }
    } catch (error) {
      if (mounted) setState(() => _error = '$error');
    }
  }

  int _countOf(Map<String, dynamic> row) {
    final id = '${row['id']}';
    return _counts['topic:$id'] ?? _counts[id] ?? 0;
  }

  Future<void> _create() async {
    final name = TextEditingController();
    String provider = 'super-brain-native';
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => StatefulBuilder(builder: (ctx, setDialog) => AlertDialog(
        backgroundColor: _panel,
        title: const Text('新建记忆空间', style: TextStyle(color: Colors.white, fontSize: 15)),
        content: Column(mainAxisSize: MainAxisSize.min, children: <Widget>[
          _field(name, '空间名称'),
          const SizedBox(height: 10),
          DropdownButton<String>(
            value: provider,
            dropdownColor: _panel,
            style: const TextStyle(color: Colors.white, fontSize: 12),
            items: const <DropdownMenuItem<String>>[
              DropdownMenuItem<String>(value: 'super-brain-native', child: Text('super-brain-native')),
              DropdownMenuItem<String>(value: 'mnemon-native', child: Text('mnemon-native')),
              DropdownMenuItem<String>(value: 'hindsight', child: Text('hindsight')),
              DropdownMenuItem<String>(value: 'mem0', child: Text('mem0')),
            ],
            onChanged: (String? v) => setDialog(() => provider = v ?? provider),
          ),
        ]),
        actions: <Widget>[
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('取消')),
          FilledButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('创建')),
        ],
      )),
    );
    if (ok != true || name.text.trim().isEmpty) return;
    await widget.client.spacesCreate(name.text.trim(), provider: provider);
    await _load();
  }

  Future<void> _delete(Map<String, dynamic> row) async {
    final id = '${row['id']}';
    if (id == 'default') return; // 默认空间禁删（后端也会再拦一次）
    final name = '${row['name']}';
    final count = _countOf(row);
    final confirm = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: _panel,
        title: const Text('删除记忆空间', style: TextStyle(color: Colors.white, fontSize: 15)),
        content: Text(
          '确定删除「$name」？\n该空间下 $count 条记忆会迁入「默认空间」，记忆正文不会丢失。',
          style: const TextStyle(color: Colors.white70, fontSize: 12),
        ),
        actions: <Widget>[
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('取消')),
          FilledButton(
            style: FilledButton.styleFrom(backgroundColor: Colors.redAccent),
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text('确认删除'),
          ),
        ],
      ),
    );
    if (confirm != true) return;
    try {
      final res = await widget.client.spacesDelete(id);
      if (mounted) {
        setState(() => _tip = res['ok'] == true
            ? '已删除「$name」：${res['migrated'] ?? 0} 条记忆迁入默认空间'
            : '删除失败：${res['error'] ?? '未知错误'}');
      }
    } catch (e) {
      if (mounted) setState(() => _tip = '删除失败：$e');
    }
    await _load();
  }

  @override
  Widget build(BuildContext context) => _frame(
        '记忆空间',
        '只有激活的空间参与召回 · Provider 可插拔（本地六引擎 / Mnemon / Hindsight / Mem0）',
        Column(children: <Widget>[
          Row(children: <Widget>[
            FilledButton(onPressed: _create, child: const Text('新建空间')),
            const SizedBox(width: 10),
            TextButton(onPressed: _load, child: const Text('刷新')),
          ]),
          if (_tip.isNotEmpty)
            Padding(padding: const EdgeInsets.only(top: 8),
                child: Text(_tip, style: const TextStyle(color: _accent, fontSize: 11))),
          if (_error.isNotEmpty)
            Padding(padding: const EdgeInsets.only(top: 8),
                child: Text('读取失败：$_error', style: const TextStyle(color: Colors.redAccent, fontSize: 11))),
          const SizedBox(height: 10),
          Expanded(child: _box(ListView.builder(
            itemCount: _spaces.length,
            itemBuilder: (ctx, index) {
              final row = Map<String, dynamic>.from(_spaces[index] as Map);
              final count = _countOf(row);
              return ListTile(
                leading: Icon(row['active'] == true ? Icons.radio_button_checked : Icons.radio_button_off,
                    color: row['active'] == true ? _accent : _hint, size: 18),
                title: Row(children: <Widget>[
                  Expanded(child: Text('${row['name']}', style: const TextStyle(color: Colors.white, fontSize: 12))),
                  Container(
                    padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
                    decoration: BoxDecoration(
                      color: count > 0 ? _accent.withValues(alpha: 0.18) : _panel2,
                      borderRadius: BorderRadius.circular(10),
                    ),
                    child: Text('$count 条',
                        style: TextStyle(color: count > 0 ? _accent : _hint, fontSize: 10)),
                  ),
                ]),
                subtitle: Text('${row['provider']} · ${row['note'] ?? ''} · 点击查看记忆',
                    style: const TextStyle(color: _hint, fontSize: 11)),
                trailing: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: <Widget>[
                    if ('${row['id']}' != 'default')
                      IconButton(
                        tooltip: '删除空间（记忆迁入默认空间）',
                        icon: const Icon(Icons.delete_outline, color: Colors.redAccent, size: 18),
                        onPressed: () => _delete(row),
                      ),
                    Switch(
                      value: row['active'] == true,
                      onChanged: (bool v) async {
                        await widget.client.spacesActivate('${row['id']}', v);
                        await _load();
                      },
                    ),
                  ],
                ),
                onTap: () async {
                  await Navigator.push(context, MaterialPageRoute<void>(
                    builder: (_) => SpaceMemoriesPage(
                      client: widget.client,
                      spaceId: '${row['id']}',
                      spaceName: '${row['name']}',
                    ),
                  ));
                  await _load(); // 返回后刷新计数
                },
              );
            },
          ))),
        ]),
      );
}

// ── 5. 技能管理 ────────────────────────────────────────────────────────
class SkillsPage extends StatefulWidget {
  const SkillsPage({super.key, required this.client});
  final BrainClient client;

  @override
  State<SkillsPage> createState() => _SkillsPageState();
}

class _SkillsPageState extends State<SkillsPage> {
  List<dynamic> _skills = <dynamic>[];
  String _error = '';

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final data = await widget.client.skillsList();
      if (mounted) setState(() => _skills = data['skills'] as List<dynamic>? ?? <dynamic>[]);
    } catch (error) {
      if (mounted) setState(() => _error = '$error');
    }
  }

  Future<void> _edit([Map<String, dynamic>? existing]) async {
    final name = TextEditingController(text: existing?['name']?.toString() ?? '');
    final desc = TextEditingController(text: existing?['description']?.toString() ?? '');
    final content = TextEditingController();
    if (existing != null) {
      final full = await widget.client.skillsGet(existing['id'].toString());
      content.text = full['content']?.toString() ?? '';
    }
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: _panel,
        title: Text(existing == null ? '新建技能' : '编辑技能（版本 +1）',
            style: const TextStyle(color: Colors.white, fontSize: 15)),
        content: SizedBox(width: 560, child: Column(mainAxisSize: MainAxisSize.min, children: <Widget>[
          _field(name, '技能名'),
          const SizedBox(height: 8),
          _field(desc, '描述'),
          const SizedBox(height: 8),
          _field(content, '技能正文 Markdown', maxLines: 6),
        ])),
        actions: <Widget>[
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('取消')),
          FilledButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('保存')),
        ],
      ),
    );
    if (ok != true || name.text.trim().isEmpty) return;
    await widget.client.skillsUpsert(
      id: existing?['id']?.toString() ?? '',
      name: name.text, description: desc.text, content: content.text,
    );
    await _load();
  }

  @override
  Widget build(BuildContext context) => _frame(
        '技能管理',
        '版本化技能 + 使用反馈自进化 · 成功率低于 50% 且使用 ≥5 次自动标记待改进',
        Column(children: <Widget>[
          Row(children: <Widget>[
            FilledButton(onPressed: () => _edit(), child: const Text('新建技能')),
            const SizedBox(width: 10),
            TextButton(onPressed: _load, child: const Text('刷新')),
          ]),
          if (_error.isNotEmpty)
            Padding(padding: const EdgeInsets.only(top: 8),
                child: Text('读取失败：$_error', style: const TextStyle(color: Colors.redAccent, fontSize: 11))),
          const SizedBox(height: 10),
          Expanded(child: _box(ListView.builder(
            itemCount: _skills.length,
            itemBuilder: (ctx, index) {
              final row = Map<String, dynamic>.from(_skills[index] as Map);
              return ListTile(
                title: Text('${row['name']}  ·  v${row['version'] ?? 1}',
                    style: const TextStyle(color: Colors.white, fontSize: 12)),
                subtitle: Text('${row['description'] ?? ''} · 使用 ${row['usage_count'] ?? 0} 次 · 成功率 ${row['success_rate'] ?? 0}',
                    style: const TextStyle(color: _hint, fontSize: 11)),
                trailing: Row(mainAxisSize: MainAxisSize.min, children: <Widget>[
                  TextButton(
                    onPressed: () async {
                      await widget.client.skillsEvolve('${row['id']}', true, note: 'UI 反馈：成功');
                      await _load();
                    },
                    child: const Text('标记成功'),
                  ),
                  TextButton(
                    onPressed: () async {
                      await widget.client.skillsEvolve('${row['id']}', false, note: 'UI 反馈：失败');
                      await _load();
                    },
                    child: const Text('标记失败'),
                  ),
                  IconButton(
                    icon: const Icon(Icons.edit_outlined, size: 16, color: _accent),
                    onPressed: () => _edit(row),
                  ),
                  IconButton(
                    tooltip: '删除技能',
                    icon: const Icon(Icons.delete_outline, size: 16, color: Colors.redAccent),
                    onPressed: () async {
                      final confirmed = await showDialog<bool>(
                        context: context,
                        builder: (ctx) => AlertDialog(
                          backgroundColor: _panel,
                          title: const Text('删除技能？',
                              style: TextStyle(color: Colors.white, fontSize: 15)),
                          content: Text('将删除「${row['name']}」的索引与正文，不可恢复。',
                              style: const TextStyle(color: Colors.white, fontSize: 12)),
                          actions: <Widget>[
                            TextButton(onPressed: () => Navigator.pop(ctx, false),
                                child: const Text('取消')),
                            FilledButton(onPressed: () => Navigator.pop(ctx, true),
                                child: const Text('删除')),
                          ],
                        ),
                      );
                      if (confirmed != true) return;
                      await widget.client.skillsDelete('${row['id']}');
                      await _load();
                    },
                  ),
                ]),
              );
            },
          ))),
        ]),
      );
}
