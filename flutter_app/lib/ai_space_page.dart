import 'dart:async';

import 'package:flutter/material.dart';

import 'brain_client.dart';

/// AI 空间 —— QQ 风格个人主页：渐变横幅头像/昵称/可编辑签名/动态·日记 tab。
/// 数据源 = 超大脑本体（RPC：ai.profile / ai.moments / ai.diary / ai.signature_write）。
/// 动态 = hyperedge 真实事件骨架；日记 = ai_narrative 叙事 neuron（巩固窗口产出）。
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
  static const Color _dead = Color(0xFFF87171);
  static const Color _accent = Color(0xFF6EE7B7);
  static const List<Color> _bannerGradient = <Color>[
    Color(0xFF3B82F6),
    Color(0xFF8B5CF6),
    Color(0xFFEC4899),
  ];

  late final TabController _tabs =
      TabController(length: 2, vsync: this);

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
          await widget.client.call('ai.moments', <String, dynamic>{'limit': 10});
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

  Future<void> _editSignature() async {
    final controller = TextEditingController(
      text: (_profile['signature'] ?? '').toString(),
    );
    final saved = await showDialog<bool>(
      context: context,
      builder: (BuildContext context) => AlertDialog(
        backgroundColor: _panel,
        title: const Text('编辑签名',
            style: TextStyle(color: Colors.white, fontSize: 16)),
        content: TextField(
          controller: controller,
          autofocus: true,
          maxLength: 120,
          style: const TextStyle(color: Colors.white, fontSize: 13),
          decoration: const InputDecoration(
            hintText: '一句话介绍自己…',
            hintStyle: TextStyle(color: _hint, fontSize: 13),
            counterStyle: TextStyle(color: _hint, fontSize: 10),
            enabledBorder: UnderlineInputBorder(
                borderSide: BorderSide(color: _border)),
            focusedBorder: UnderlineInputBorder(
                borderSide: BorderSide(color: _accent)),
          ),
        ),
        actions: <Widget>[
          TextButton(
            onPressed: () => Navigator.of(context).pop(false),
            child: const Text('取消', style: TextStyle(color: _hint)),
          ),
          TextButton(
            onPressed: () => Navigator.of(context).pop(true),
            child: const Text('保存', style: TextStyle(color: _accent)),
          ),
        ],
      ),
    );
    controller.dispose();
    if (saved != true) return;
    try {
      final result = await widget.client.call('ai.signature_write',
          <String, dynamic>{'signature': controller.text});
      if (!mounted) return;
      if (result['ok'] == true) {
        setState(() => _profile['signature'] = result['signature']);
      } else {
        setState(() => _message = '签名保存失败：${result['error'] ?? '未知原因'}');
      }
    } catch (error) {
      if (mounted) setState(() => _message = '签名保存失败：$error');
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

  // ── 渐变横幅：头像 + 昵称 + 签名 ─────────────────────────────────────
  Widget _banner() {
    final signature = (_profile['signature'] ?? '').toString();
    final diaryCount = (_profile['diary_count'] ?? 0) as num;
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
          height: 56,
          alignment: Alignment.centerLeft,
          padding: const EdgeInsets.symmetric(horizontal: 16),
          child: const Text('AI 空间',
              style: TextStyle(
                  color: Colors.white70, fontSize: 13, letterSpacing: 2)),
        ),
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 0, 16, 14),
          child: Row(crossAxisAlignment: CrossAxisAlignment.end, children: <Widget>[
            Container(
              width: 72,
              height: 72,
              decoration: BoxDecoration(
                color: _bg,
                shape: BoxShape.circle,
                border: Border.all(color: Colors.white, width: 2),
              ),
              alignment: Alignment.center,
              child: Text((_profile['avatar_emoji'] ?? '🐟').toString(),
                  style: const TextStyle(fontSize: 36)),
            ),
            const SizedBox(width: 14),
            Expanded(
              child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: <Widget>[
                    Row(children: <Widget>[
                      Text((_profile['nickname'] ?? '大肥鱼').toString(),
                          style: const TextStyle(
                              color: Colors.white,
                              fontSize: 20,
                              fontWeight: FontWeight.bold)),
                      const SizedBox(width: 8),
                      Container(
                        padding: const EdgeInsets.symmetric(
                            horizontal: 8, vertical: 2),
                        decoration: BoxDecoration(
                          color: Colors.white24,
                          borderRadius: BorderRadius.circular(999),
                        ),
                        child: Text('日记 $diaryCount 篇',
                            style: const TextStyle(
                                color: Colors.white, fontSize: 10)),
                      ),
                    ]),
                    const SizedBox(height: 6),
                    InkWell(
                      onTap: _busy ? null : _editSignature,
                      child: Row(children: <Widget>[
                        Expanded(
                          child: Text(
                            signature.isEmpty ? '点击设置签名…' : signature,
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: TextStyle(
                                color: signature.isEmpty
                                    ? Colors.white60
                                    : Colors.white,
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

  // ── 动态卡片：hyperedge 事件骨架 ────────────────────────────────────
  Widget _momentCard(Map<String, dynamic> moment) {
    final text = (moment['text'] ?? '').toString();
    final time = _fmtTime(moment['ts_ms']);
    return Container(
      margin: const EdgeInsets.fromLTRB(16, 8, 16, 0),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: _panel,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: _border),
      ),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: <Widget>[
        Row(children: <Widget>[
          const Text('🐟',
              style: TextStyle(fontSize: 14)),
          const SizedBox(width: 6),
          Text((moment['label'] ?? '一件事').toString(),
              style: const TextStyle(
                  color: _accent, fontSize: 12,
                  fontWeight: FontWeight.w600)),
          const Spacer(),
          Text(time, style: const TextStyle(color: _hint, fontSize: 10)),
        ]),
        if (text.isNotEmpty) ...<Widget>[
          const SizedBox(height: 8),
          Text(text,
              style: const TextStyle(color: Colors.white, fontSize: 12, height: 1.5)),
        ],
      ]),
    );
  }

  // ── 日记卡片：ai_narrative 叙事 neuron ──────────────────────────────
  Widget _diaryCard(Map<String, dynamic> entry) {
    final text = (entry['text'] ?? '').toString();
    final topic = (entry['topic'] ?? '').toString();
    final time = _fmtTime(entry['ts_ms']);
    return Container(
      margin: const EdgeInsets.fromLTRB(16, 8, 16, 0),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: _panel,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: _border),
      ),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: <Widget>[
        Row(children: <Widget>[
          const Icon(Icons.auto_stories, color: _accent, size: 14),
          const SizedBox(width: 6),
          if (topic.isNotEmpty)
            Text('#$topic',
                style: const TextStyle(
                    color: _accent, fontSize: 12,
                    fontWeight: FontWeight.w600)),
          const Spacer(),
          Text(time, style: const TextStyle(color: _hint, fontSize: 10)),
        ]),
        const SizedBox(height: 8),
        Text(text,
            style: const TextStyle(color: Colors.white, fontSize: 12, height: 1.6)),
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
                  style: const TextStyle(color: _dead, fontSize: 11)),
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
              Tab(text: '动态'),
              Tab(text: '日记'),
            ],
          ),
          const Spacer(),
          IconButton(
            tooltip: '刷新',
            onPressed: _busy ? null : _load,
            icon: const Icon(Icons.refresh, color: _hint, size: 20),
          ),
        ]),
        Expanded(
          child: TabBarView(
            controller: _tabs,
            children: <Widget>[
              // 动态 tab
              _busy
                  ? const Center(
                      child: CircularProgressIndicator(color: _accent))
                  : emptyMoments
                      ? const Center(
                          child: Text('暂无动态——事件骨架巩固后会出现在这里。',
                              style: TextStyle(color: _hint, fontSize: 12)))
                      : ListView(
                          padding: const EdgeInsets.only(bottom: 12),
                          children: <Widget>[
                            for (final m in _moments) _momentCard(m),
                          ],
                        ),
              // 日记 tab
              _busy
                  ? const Center(
                      child: CircularProgressIndicator(color: _accent))
                  : emptyDiary
                      ? const Center(
                          child: Text('暂无日记——AI 第一人称叙事会在再巩固窗口产出。',
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
