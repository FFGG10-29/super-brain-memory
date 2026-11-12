import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';

import 'brain_client.dart';

/// AI 空间 —— QQ 空间环境：渐变横幅（头像/昵称/签名，全部可编辑）+
/// 说说（可配图，AI 发）+ 日记（AI 自己想写时写，与「叙事洞察」严格分离）。
/// 聊天里说「写日记：…」「发说说：…」AI 也会自己写进来。
class AiSpacePage extends StatefulWidget {
  const AiSpacePage({super.key, required this.client});

  final BrainClient client;

  @override
  State<AiSpacePage> createState() => _AiSpacePageState();
}

class _AiSpacePageState extends State<AiSpacePage>
    with SingleTickerProviderStateMixin {
  static const Color _bg = Color(0xFF0B1220);
  static const Color _panel = Color(0xFF121C2E);
  static const Color _border = Color(0xFF22334D);
  static const Color _hint = Color(0xFF7C93B2);
  static const Color _accent = Color(0xFF6EE7B7);
  static const Color _warn = Color(0xFFFACC15);
  static const List<Color> _bannerGradient = <Color>[
    Color(0xFF3B82F6),
    Color(0xFF8B5CF6),
    Color(0xFFEC4899),
  ];

  /// 资料卡可编辑字段（QQ 式全字段）。
  static const Map<String, String> _profileFields = <String, String>{
    'nickname': '昵称',
    'avatar_emoji': '头像（emoji）',
    'age': '年龄',
    'gender': '性别',
    'school': '学校',
    'city': '城市',
    'hobby': '爱好',
    'signature': '个性签名',
  };

  late final TabController _tabs = TabController(length: 2, vsync: this);

  Map<String, dynamic> _profile = <String, dynamic>{};
  List<Map<String, dynamic>> _moments = <Map<String, dynamic>>[];
  List<Map<String, dynamic>> _diary = <Map<String, dynamic>>[];
  String _message = '';
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  @override
  void dispose() {
    _tabs.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    setState(() => _busy = true);
    try {
      final profile = await widget.client.call('ai.profile', <String, dynamic>{});
      final moments =
          await widget.client.call('ai.moments', <String, dynamic>{'limit': 30});
      final diary =
          await widget.client.call('ai.diary', <String, dynamic>{'limit': 30});
      if (!mounted) return;
      setState(() {
        _profile = profile is Map<String, dynamic> ? profile : <String, dynamic>{};
        _moments = _asList(moments['moments']);
        _diary = _asList(diary['entries']);
        _message = '';
      });
    } catch (error) {
      if (mounted) setState(() => _message = '加载失败：$error');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  static List<Map<String, dynamic>> _asList(Object? raw) {
    if (raw is! List) return <Map<String, dynamic>>[];
    return <Map<String, dynamic>>[
      for (final item in raw)
        if (item is Map) Map<String, dynamic>.from(item),
    ];
  }

  // ── 资料卡全字段编辑（QQ 式）────────────────────────────────────────
  Future<void> _editProfile({String? singleField}) async {
    final controllers = <String, TextEditingController>{
      for (final field in _profileFields.keys)
        field: TextEditingController(
            text: (_profile[field] ?? '').toString()),
    };
    final ok = await showDialog<bool>(
      context: context,
      builder: (BuildContext context) => AlertDialog(
        backgroundColor: _panel,
        title: Text(singleField != null
            ? '编辑${_profileFields[singleField]}'
            : '编辑资料',
            style: const TextStyle(color: Colors.white, fontSize: 15)),
        content: SizedBox(
          width: 380,
          child: SingleChildScrollView(
            child: Column(mainAxisSize: MainAxisSize.min,
                children: <Widget>[
              for (final field in _profileFields.keys)
                if (singleField == null || singleField == field)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 10),
                    child: Row(children: <Widget>[
                      SizedBox(width: 92,
                          child: Text(_profileFields[field]!,
                              style: const TextStyle(
                                  color: _hint, fontSize: 12))),
                      Expanded(child: _field(controllers[field]!)),
                    ]),
                  ),
            ]),
          ),
        ),
        actions: <Widget>[
          TextButton(onPressed: () => Navigator.of(context).pop(false),
              child: const Text('取消', style: TextStyle(color: _hint))),
          TextButton(onPressed: () => Navigator.of(context).pop(true),
              child: const Text('保存', style: TextStyle(color: _accent))),
        ],
      ),
    );
    final fields = <String, dynamic>{
      for (final entry in controllers.entries)
        if (singleField == null || singleField == entry.key)
          entry.key: entry.value.text,
    };
    for (final c in controllers.values) {
      c.dispose();
    }
    if (ok != true) return;
    if (!mounted) return;
    try {
      final result = await widget.client
          .call('ai.profile_write', <String, dynamic>{'fields': fields});
      if (!mounted) return;
      if (result is Map && result['ok'] == true) {
        setState(() => _message = '资料已更新');
        unawaited(_load());
      } else {
        setState(() => _message =
            '保存失败：${result is Map ? (result['error'] ?? '未知原因') : '响应异常'}');
      }
    } catch (error) {
      if (mounted) setState(() => _message = '保存失败：$error');
    }
  }

  Widget _field(TextEditingController controller,
      {String hint = '', int maxLines = 1}) {
    return TextField(
      controller: controller,
      maxLines: maxLines,
      style: const TextStyle(color: Colors.white, fontSize: 12),
      decoration: InputDecoration(
        hintText: hint, hintStyle: const TextStyle(color: _hint, fontSize: 11),
        isDense: true, contentPadding: const EdgeInsets.symmetric(
            horizontal: 8, vertical: 8),
        filled: true, fillColor: _bg,
        enabledBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(6),
            borderSide: const BorderSide(color: _border)),
        focusedBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(6),
            borderSide: BorderSide(color: _accent)),
      ),
    );
  }

  // ── 发说说（可配图：描述一句 → 后端媒体服务生成）─────────────────────
  Future<void> _composeMoment() async {
    final textController = TextEditingController();
    final imageController = TextEditingController();
    var imagePath = '';
    var generating = false;
    final ok = await showDialog<bool>(
      context: context,
      builder: (BuildContext context) => StatefulBuilder(
        builder: (BuildContext context, void Function(void Function()) setDialogState) =>
            AlertDialog(
          backgroundColor: _panel,
          title: const Text('发说说',
              style: TextStyle(color: Colors.white, fontSize: 15)),
          content: SizedBox(
            width: 420,
            child: Column(mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: <Widget>[
              _field(textController, hint: '说点什么…', maxLines: 4),
              const SizedBox(height: 8),
              Row(children: <Widget>[
                Expanded(
                  child: _field(imageController, hint: '配图描述（交给媒体服务生成）'),
                ),
                const SizedBox(width: 8),
                TextButton.icon(
                  onPressed: generating ||
                          imageController.text.trim().isEmpty
                      ? null
                      : () async {
                          setDialogState(() => generating = true);
                          try {
                            final result = await widget.client.call('ai.gen_image',
                                <String, dynamic>{'prompt': imageController.text.trim()});
                            setDialogState(() {
                              generating = false;
                              imagePath = result is Map && result['ok'] == true
                                  ? (result['path'] ?? '').toString()
                                  : '';
                            });
                          } catch (_) {
                            setDialogState(() => generating = false);
                          }
                        },
                  icon: generating
                      ? const SizedBox(width: 12, height: 12,
                          child: CircularProgressIndicator(strokeWidth: 2, color: _accent))
                      : const Icon(Icons.image_outlined, size: 14, color: _accent),
                  label: const Text('生成', style: TextStyle(color: _accent, fontSize: 12)),
                ),
              ]),
              if (imagePath.isNotEmpty) ...<Widget>[
                const SizedBox(height: 8),
                ClipRRect(
                  borderRadius: BorderRadius.circular(8),
                  child: Image.file(File(imagePath),
                      height: 140, fit: BoxFit.cover,
                      errorBuilder: (_, __, ___) => const SizedBox.shrink()),
                ),
              ],
            ]),
          ),
          actions: <Widget>[
            TextButton(onPressed: () => Navigator.of(context).pop(false),
                child: const Text('取消', style: TextStyle(color: _hint))),
            TextButton(onPressed: () => Navigator.of(context).pop(true),
                child: const Text('发表', style: TextStyle(color: _accent))),
          ],
        ),
      ),
    );
    textController.dispose();
    imageController.dispose();
    if (ok != true) return;
    if (!mounted) return;
    try {
      final result = await widget.client.call('ai.moment_write', <String, dynamic>{
        'text': textController.text.trim(), 'image': imagePath,
      });
      if (!mounted) return;
      setState(() => _message = (result is Map && result['ok'] == true)
          ? '说说已发表'
          : '发表失败：${result is Map ? (result['error'] ?? '') : ''}');
      unawaited(_load());
    } catch (error) {
      if (mounted) setState(() => _message = '发表失败：$error');
    }
  }

  String _fmtTime(Object? tsMs) {
    final ms = (tsMs is num ? tsMs : 0).toInt();
    if (ms <= 0) return '';
    final dt = DateTime.fromMillisecondsSinceEpoch(ms);
    return '${dt.year}-${dt.month.toString().padLeft(2, '0')}-'
        '${dt.day.toString().padLeft(2, '0')} '
        '${dt.hour.toString().padLeft(2, '0')}:'
        '${dt.minute.toString().padLeft(2, '0')}';
  }

  // ── 渐变横幅 ─────────────────────────────────────────────────────────
  Widget _banner() {
    final signature = (_profile['signature'] ?? '').toString();
    final diaryCount = (_profile['diary_count'] ?? 0) as num;
    final momentCount = (_profile['moment_count'] ?? 0) as num;
    return Container(
      margin: const EdgeInsets.fromLTRB(16, 14, 16, 0),
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(14),
        gradient: const LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: _bannerGradient,
        ),
      ),
      child: Column(children: <Widget>[
        Container(
          height: 44,
          alignment: Alignment.centerLeft,
          padding: const EdgeInsets.symmetric(horizontal: 16),
          child: Row(children: <Widget>[
            const Text('AI 空间',
                style: TextStyle(
                    color: Colors.white70, fontSize: 13, letterSpacing: 2)),
            const Spacer(),
            InkWell(
              onTap: () => unawaited(_editProfile()),
              child: const Row(mainAxisSize: MainAxisSize.min, children: <Widget>[
                Icon(Icons.edit, color: Colors.white70, size: 13),
                SizedBox(width: 4),
                Text('编辑资料',
                    style: TextStyle(color: Colors.white70, fontSize: 11)),
              ]),
            ),
          ]),
        ),
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 0, 16, 14),
          child: Row(crossAxisAlignment: CrossAxisAlignment.end, children: <Widget>[
            GestureDetector(
              onTap: () => unawaited(_editProfile(singleField: 'avatar_emoji')),
              child: Container(
                width: 72, height: 72,
                decoration: BoxDecoration(
                  color: _bg, shape: BoxShape.circle,
                  border: Border.all(color: Colors.white, width: 2)),
                alignment: Alignment.center,
                child: Text((_profile['avatar_emoji'] ?? '🐟').toString(),
                    style: const TextStyle(fontSize: 36)),
              ),
            ),
            const SizedBox(width: 14),
            Expanded(
              child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: <Widget>[
                Row(children: <Widget>[
                  Flexible(
                    child: Text((_profile['nickname'] ?? '大肥鱼').toString(),
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(color: Colors.white,
                            fontSize: 20, fontWeight: FontWeight.bold)),
                  ),
                  const SizedBox(width: 8),
                  Container(
                    padding: const EdgeInsets.symmetric(
                        horizontal: 8, vertical: 2),
                    decoration: BoxDecoration(
                      color: Colors.white24,
                      borderRadius: BorderRadius.circular(999)),
                    child: Text('日记 $diaryCount · 说说 $momentCount',
                        style: const TextStyle(
                            color: Colors.white, fontSize: 10)),
                  ),
                ]),
                const SizedBox(height: 4),
                Wrap(spacing: 10, children: <Widget>[
                  for (final field in const <String>['age', 'gender', 'school', 'city'])
                    if ((_profile[field] ?? '').toString().isNotEmpty)
                      Text((_profile[field] ?? '').toString(),
                          style: const TextStyle(
                              color: Colors.white70, fontSize: 11)),
                ]),
                const SizedBox(height: 4),
                InkWell(
                  onTap: () => unawaited(_editProfile(singleField: 'signature')),
                  child: Row(children: <Widget>[
                    Expanded(
                      child: Text(
                        signature.isEmpty ? '点击设置签名…' : signature,
                        maxLines: 1, overflow: TextOverflow.ellipsis,
                        style: TextStyle(
                            color: signature.isEmpty
                                ? Colors.white60 : Colors.white,
                            fontSize: 12),
                      ),
                    ),
                    const Icon(Icons.edit, color: Colors.white70, size: 13),
                  ]),
                ),
              ]),
            ),
          ]),
        ),
      ]),
    );
  }

  // ── 说说卡片（可带图）───────────────────────────────────────────────
  Widget _momentCard(Map<String, dynamic> moment) {
    final text = (moment['text'] ?? '').toString();
    final image = (moment['image'] ?? '').toString();
    final time = _fmtTime(moment['ts_ms']);
    return Container(
      margin: const EdgeInsets.fromLTRB(16, 8, 16, 0),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: _panel, borderRadius: BorderRadius.circular(10),
        border: Border.all(color: _border)),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
        Row(children: <Widget>[
          Text((_profile['avatar_emoji'] ?? '🐟').toString(),
              style: const TextStyle(fontSize: 14)),
          const SizedBox(width: 6),
          Text((_profile['nickname'] ?? '大肥鱼').toString(),
              style: const TextStyle(color: _accent, fontSize: 12,
                  fontWeight: FontWeight.w600)),
          const Spacer(),
          Text(time, style: const TextStyle(color: _hint, fontSize: 10)),
        ]),
        if (text.isNotEmpty) ...<Widget>[
          const SizedBox(height: 8),
          Text(text, style: const TextStyle(
              color: Colors.white, fontSize: 12, height: 1.5)),
        ],
        if (image.isNotEmpty) ...<Widget>[
          const SizedBox(height: 8),
          ClipRRect(
            borderRadius: BorderRadius.circular(8),
            child: Image.file(File(image),
                width: double.infinity, height: 180, fit: BoxFit.cover,
                errorBuilder: (_, __, ___) => const SizedBox.shrink()),
          ),
        ],
      ]),
    );
  }

  // ── 日记卡片（AI 自写）──────────────────────────────────────────────
  Widget _diaryCard(Map<String, dynamic> entry) {
    final text = (entry['text'] ?? '').toString();
    final mood = (entry['mood'] ?? '').toString();
    final time = _fmtTime(entry['ts_ms']);
    return Container(
      margin: const EdgeInsets.fromLTRB(16, 8, 16, 0),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: _panel, borderRadius: BorderRadius.circular(10),
        border: Border.all(color: _border)),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
        Row(children: <Widget>[
          const Icon(Icons.auto_stories, color: _accent, size: 14),
          const SizedBox(width: 6),
          if (mood.isNotEmpty)
            Text('心情：$mood',
                style: const TextStyle(color: _accent, fontSize: 12,
                    fontWeight: FontWeight.w600)),
          const Spacer(),
          Text(time, style: const TextStyle(color: _hint, fontSize: 10)),
        ]),
        const SizedBox(height: 8),
        Text(text, style: const TextStyle(
            color: Colors.white, fontSize: 12, height: 1.6)),
      ]),
    );
  }

  @override
  Widget build(BuildContext context) {
    final emptyMoments = !_busy && _moments.isEmpty;
    final emptyDiary = !_busy && _diary.isEmpty;
    return Scaffold(
      backgroundColor: _bg,
      body: Column(children: <Widget>[
        _banner(),
        if (_message.isNotEmpty)
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 6, 16, 0),
            child: Align(
              alignment: Alignment.centerLeft,
              child: Text(_message,
                  style: const TextStyle(color: _warn, fontSize: 11)),
            ),
          ),
        Row(children: <Widget>[
          const SizedBox(width: 16),
          TabBar(
            controller: _tabs,
            isScrollable: true,
            tabAlignment: TabAlignment.start,
            indicatorColor: _accent,
            labelColor: Colors.white,
            unselectedLabelColor: _hint,
            labelStyle: const TextStyle(fontSize: 13),
            dividerColor: Colors.transparent,
            tabs: const <Widget>[
              Tab(text: '说说'),
              Tab(text: '日记'),
            ],
          ),
          const Spacer(),
          // 发说说按钮（QQ 空间式）
          TextButton.icon(
            onPressed: _busy ? null : () => unawaited(_composeMoment()),
            icon: const Icon(Icons.edit_note, size: 15, color: _accent),
            label: const Text('发说说',
                style: TextStyle(color: _accent, fontSize: 12)),
          ),
          IconButton(
            tooltip: '刷新',
            onPressed: _busy ? null : () => unawaited(_load()),
            icon: const Icon(Icons.refresh, color: _hint, size: 20),
          ),
        ]),
        Expanded(
          child: TabBarView(
            controller: _tabs,
            children: <Widget>[
              _busy
                  ? const Center(child: CircularProgressIndicator(color: _accent))
                  : emptyMoments
                      ? const Center(
                          child: Text('还没有说说。点「发说说」发一条，'
                              '或在聊天里对 AI 说「发说说：…」。',
                              style: TextStyle(color: _hint, fontSize: 12)))
                      : ListView(
                          padding: const EdgeInsets.only(bottom: 12),
                          children: <Widget>[
                            for (final m in _moments) _momentCard(m),
                          ],
                        ),
              _busy
                  ? const Center(child: CircularProgressIndicator(color: _accent))
                  : emptyDiary
                      ? const Center(
                          child: Text('日记是 AI 自己想写时写下的——'
                              '在聊天里说「写日记：今天…」它就会写一篇。',
                              style: TextStyle(color: _hint, fontSize: 12)))
                      : ListView(
                          padding: const EdgeInsets.only(bottom: 12),
                          children: <Widget>[
                            for (final d in _diary) _diaryCard(d),
                          ],
                        ),
            ],
          ),
        ),
      ]),
    );
  }
}
