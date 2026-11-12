import 'dart:async';

import 'package:flutter/material.dart';

import 'brain_client.dart';
import 'chat_service.dart';

/// 聊天入口测试页 —— 微信风格（浅色、绿/白气泡、顶栏"大肥鱼"）。
/// 数据全部来自 8769 真实通道：hello 鉴权、typing、reply、error 帧。
class ChatPage extends StatefulWidget {
  const ChatPage({super.key, required this.client, this.onOpenSpace});

  final BrainClient client;

  /// 头像资料卡 →「进入 AI 空间」由外壳跳转（第 28 页）。
  final VoidCallback? onOpenSpace;

  @override
  State<ChatPage> createState() => _ChatPageState();
}

class _ChatPageState extends State<ChatPage> {
  final TextEditingController _input = TextEditingController();
  final ScrollController _scroll = ScrollController();
  final List<_Msg> _messages = <_Msg>[];
  ChatService? _service;
  ChatLinkState _link = ChatLinkState.disconnected;
  String _tokenError = '';
  String _sessionId = '';
  // 模型选择：'' = 跟随路由；free:* = 免密；其余 = WorkBuddy 模型
  String _selectedModel = '';
  bool _showEmojiPanel = false;

  /// 表情包（emoji 面板）：点击插入输入框，长按直接发送。
  static const List<String> _emojiSet = <String>[
    '😊', '😂', '🤣', '😍', '😘', '😎', '🤔', '😴', '😭', '😡',
    '🥳', '😱', '🤗', '🤫', '🤩', '😈', '🙃', '😐', '🙄', '😶',
    '👍', '👎', '👏', '🙏', '💪', '🤝', '✌️', '🤙', '👌', '🫶',
    '❤️', '💔', '💖', '💘', '🌸', '🌹', '🌻', '🎁', '🎉', '🎂',
    '🐟', '🐬', '🐳', '🐠', '🐡', '🦈', '🐙', '🦀', '🌊', '⭐',
    '🍅', '🍜', '🍚', '🍰', '🍵', '🍺', '🀄', '🎮', '😴', '💤',
  ];
  List<Map<String, dynamic>> _wbModels = <Map<String, dynamic>>[];
  List<Map<String, dynamic>> _freeModels = <Map<String, dynamic>>[];
  bool _typing = false;
  StreamSubscription<Map<String, dynamic>>? _framesSub;
  StreamSubscription<ChatLinkState>? _stateSub;

  static const Color _bg = Color(0xFFEDEDED);
  static const Color _bar = Color(0xFFEDEDED);
  static const Color _mine = Color(0xFF95EC69);
  static const Color _other = Color(0xFFFFFFFF);
  static const Color _text = Color(0xFF191919);
  static const Color _hint = Color(0xFF9B9B9B);

  @override
  void initState() {
    super.initState();
    unawaited(_boot());
  }

  Future<void> _boot() async {
    try {
      final data = await widget.client.call('chat.token', <String, dynamic>{});
      final token = (data is Map && data['ok'] == true)
          ? (data['token'] ?? '').toString()
          : '';
      if (token.isEmpty) {
        setState(() => _tokenError =
            'chat 令牌获取失败：${data is Map ? (data['error'] ?? '令牌为空') : '响应异常'}');
        return;
      }
      final service = ChatService();
      _service = service;
      _framesSub = service.frames.listen(_onFrame);
      _stateSub = service.states.listen((state) {
        if (mounted) setState(() => _link = state);
      });
      await service.connect(token);
    } catch (error) {
      if (mounted) setState(() => _tokenError = '连接失败：$error');
    }
  }

  void _onFrame(Map<String, dynamic> frame) {
    final type = frame['type'];
    if (type == 'hello_ack') {
      _sessionId = (frame['session_id'] ?? '').toString();
      unawaited(_loadHistory());
      unawaited(_loadModels());
    } else if (type == 'typing') {
      final state = frame['state'];
      if (mounted) setState(() => _typing = state != false);
    } else if (type == 'reply') {
      if (mounted) {
        setState(() {
          _typing = false;
          _messages.add(_Msg(
              text: (frame['text'] ?? '').toString(),
              mine: false,
              model: (frame['model'] ?? '').toString(),
              kind: (frame['provider_kind'] ?? '').toString(),
              fallback: frame['fallback']));
        });
        unawaited(_scrollDown());
      }
    } else if (type == 'error') {
      if (mounted) {
        setState(() {
          _typing = false;
          _messages.add(_Msg(text: (frame['message'] ?? '出错了').toString(),
              mine: false, model: '', kind: 'error', fallback: null));
        });
        unawaited(_scrollDown());
      }
    }
  }

  /// 模型清单（号池目录：WorkBuddy 启用模型 + 免费模型），进页面拉一次。
  Future<void> _loadModels() async {
    try {
      final data = await widget.client.call('pool.catalog', <String, dynamic>{});
      if (!mounted || data is! Map) return;
      final disabled = ((data['disabled_models'] as List<dynamic>? ?? <dynamic>[]))
          .map((e) => e.toString())
          .toSet();
      final wb = (data['wb'] as Map? ?? {});
      final free = (data['free'] as Map? ?? {});
      String nameOf(dynamic row) {
        if (row is! Map) return '';
        final n = (row['name'] ?? '').toString();
        final mult = row['multiplier'];
        final tag = mult is num ? '（x${mult.toStringAsFixed(2)}）' : '';
        return '$n$tag';
      }
      // 不同供应商同名模型不去重，加 id 标识区分（hy3/hy3-b…）
      final wbRows = <Map<String, dynamic>>[
        for (final row in (wb['models'] as List<dynamic>? ?? <dynamic>[]))
          if (row is Map && !disabled.contains((row['id'] ?? '').toString()))
            <String, dynamic>{
              'id': (row['id'] ?? '').toString(),
              'name': nameOf(row),
            },
      ];
      final nameCounts = <String, int>{};
      for (final m in wbRows) {
        nameCounts[m['name'] as String] =
            (nameCounts[m['name'] as String] ?? 0) + 1;
      }
      for (final m in wbRows) {
        if ((nameCounts[m['name'] as String] ?? 0) > 1) {
          m['name'] = '${m['name']} · ${m['id']}';
        }
      }
      setState(() {
        _wbModels = wbRows;
        _freeModels = <Map<String, dynamic>>[
          for (final row in (free['models'] as List<dynamic>? ?? <dynamic>[]))
            if (row is Map)
              <String, dynamic>{
                'id': 'free:${(row['id'] ?? '').toString()}',
                'name': nameOf(row),
              },
        ];
      });
    } catch (_) {
      // 目录拉不到也能聊天（跟随路由）
    }
  }

  /// hello_ack 后回放持久化记录（sessions.db，跨重启）；只在会话首帧拉一次。
  Future<void> _loadHistory() async {
    if (_sessionId.isEmpty || _messages.isNotEmpty) return;
    try {
      final data = await widget.client.call('chat.history', <String, dynamic>{
        'session_id': _sessionId, 'limit': 200,
      });
      if (!mounted || _messages.isNotEmpty) return;
      final rows = data is Map && data['ok'] == true
          ? (data['messages'] as List? ?? <dynamic>[])
          : <dynamic>[];
      if (rows.isEmpty) return;
      setState(() {
        for (final row in rows) {
          if (row is! Map) continue;
          _messages.add(_Msg(
            text: (row['text'] ?? '').toString(),
            mine: (row['role'] ?? '').toString() == 'user',
            model: '', kind: '', fallback: null,
          ));
        }
      });
      unawaited(_scrollDown());
    } catch (_) {
      // 历史拉取失败不阻断聊天
    }
  }

  /// 头像 → 资料卡（ai.profile 真数据）+ AI 空间入口。
  Future<void> _showProfileCard() async {
    Map<String, dynamic> profile = <String, dynamic>{};
    try {
      final data = await widget.client.call('ai.profile', <String, dynamic>{});
      if (data is Map && data['ok'] == true) {
        profile = Map<String, dynamic>.from(data);
      }
    } catch (_) {}
    if (!mounted) return;
    await showModalBottomSheet<void>(
      context: context,
      backgroundColor: Colors.white,
      shape: const RoundedRectangleBorder(
          borderRadius: BorderRadius.vertical(top: Radius.circular(16))),
      builder: (BuildContext context) => SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(20, 20, 20, 16),
          child: Column(mainAxisSize: MainAxisSize.min, children: <Widget>[
            CircleAvatar(
              radius: 34,
              backgroundColor: const Color(0xFF4A90D9),
              child: Text((profile['avatar_emoji'] ?? '🐟').toString(),
                  style: const TextStyle(fontSize: 30)),
            ),
            const SizedBox(height: 10),
            Text((profile['nickname'] ?? '大肥鱼').toString(),
                style: const TextStyle(
                    color: _text, fontSize: 18, fontWeight: FontWeight.bold)),
            const SizedBox(height: 4),
            Text(
              (profile['signature'] ?? '').toString().isEmpty
                  ? '这个家伙很懒，什么都没留下'
                  : (profile['signature'] ?? '').toString(),
              style: const TextStyle(color: _hint, fontSize: 12),
            ),
            const SizedBox(height: 6),
            Text('日记 ${(profile['diary_count'] ?? 0)} 篇',
                style: const TextStyle(color: _hint, fontSize: 11)),
            const SizedBox(height: 14),
            SizedBox(
              width: double.infinity,
              child: Material(
                color: const Color(0xFF07C160),
                borderRadius: BorderRadius.circular(8),
                child: InkWell(
                  borderRadius: BorderRadius.circular(8),
                  onTap: () {
                    Navigator.of(context).pop();
                    widget.onOpenSpace?.call();
                  },
                  child: const Padding(
                    padding: EdgeInsets.symmetric(vertical: 11),
                    child: Center(
                      child: Text('进入 AI 空间（动态 · 日记）',
                          style: TextStyle(color: Colors.white, fontSize: 14)),
                    ),
                  ),
                ),
              ),
            ),
          ]),
        ),
      ),
    );
  }

  Future<void> _scrollDown() async {
    await Future<void>.delayed(const Duration(milliseconds: 60));
    if (_scroll.hasClients) {
      await _scroll.animateTo(_scroll.position.maxScrollExtent,
          duration: const Duration(milliseconds: 200), curve: Curves.easeOut);
    }
  }

  void _send() {
    final text = _input.text.trim();
    if (text.isEmpty || _service == null) return;
    _service!.sendMessage(text, model: _selectedModel.isEmpty ? null : _selectedModel);
    setState(() {
      _messages.add(_Msg(text: text, mine: true, model: '', kind: '', fallback: null));
      _typing = true;
      _input.clear();
    });
    unawaited(_scrollDown());
  }

  String _linkText() {
    switch (_link) {
      case ChatLinkState.connected:
        return '已连接';
      case ChatLinkState.connecting:
      case ChatLinkState.reconnecting:
        return '连接中…';
      case ChatLinkState.disconnected:
        return _tokenError.isEmpty ? '已断开' : _tokenError;
    }
  }

  Color _linkColor() {
    switch (_link) {
      case ChatLinkState.connected:
        return const Color(0xFF07C160);
      case ChatLinkState.connecting:
      case ChatLinkState.reconnecting:
        return _hint;
      case ChatLinkState.disconnected:
        return const Color(0xFFE64340);
    }
  }

  // 微信风格气泡：自己绿色靠右、对方白色靠左，圆角 8 带小尖角感。
  Widget _bubble(_Msg msg) {
    final mine = msg.mine;
    final tags = <String>[
      if (!mine && msg.model.isNotEmpty) msg.model,
      if (!mine && msg.kind.isNotEmpty) msg.kind,
      if (msg.fallback != null && msg.fallback != false) '兜底回复',
    ];
    return Padding(
      padding: const EdgeInsets.fromLTRB(10, 6, 10, 6),
      child: Row(
        mainAxisAlignment:
            mine ? MainAxisAlignment.end : MainAxisAlignment.start,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: <Widget>[
          if (!mine) _avatar(mine),
          if (!mine) const SizedBox(width: 8),
          Flexible(
            child: Container(
              constraints: const BoxConstraints(maxWidth: 280),
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 9),
              decoration: BoxDecoration(
                color: msg.kind == 'error'
                    ? const Color(0xFFFDE7E7)
                    : (mine ? _mine : _other),
                borderRadius: BorderRadius.circular(8),
              ),
              child: Column(crossAxisAlignment: CrossAxisAlignment.start,
                  children: <Widget>[
                Text(msg.text,
                    style: const TextStyle(color: _text, fontSize: 15, height: 1.35)),
                if (tags.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 4),
                    child: Text(tags.join(' · '),
                        style: const TextStyle(color: _hint, fontSize: 10)),
                  ),
              ]),
            ),
          ),
          if (mine) const SizedBox(width: 8),
          if (mine) _avatar(mine),
        ],
      ),
    );
  }

  Widget _avatar(bool mine) {
    final circle = CircleAvatar(
      radius: 18,
      backgroundColor: mine ? const Color(0xFF07C160) : const Color(0xFF4A90D9),
      child: Text(mine ? '我' : '🐟',
          style: const TextStyle(color: Colors.white, fontSize: 13)),
    );
    if (mine) return circle;
    // 大肥鱼头像可点：资料卡 + AI 空间入口
    return GestureDetector(onTap: _showProfileCard, child: circle);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: _bg,
      appBar: AppBar(
        backgroundColor: _bar,
        elevation: 0.5,
        centerTitle: true,
        title: Column(children: <Widget>[
          const Text('大肥鱼',
              style: TextStyle(color: _text, fontSize: 17)),
          Text(_linkText(),
              style: TextStyle(color: _linkColor(), fontSize: 10)),
        ]),
        iconTheme: const IconThemeData(color: _text),
        actions: <Widget>[
          // 模型选择（AstrBot 式）：跟随路由 / WorkBuddy 模型 / 免费模型
          PopupMenuButton<String>(
            tooltip: '选择回复模型',
            onSelected: (value) => setState(() => _selectedModel = value),
            color: Colors.white,
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 10),
              child: Row(mainAxisSize: MainAxisSize.min, children: <Widget>[
                Icon(Icons.smart_toy_outlined,
                    size: 16, color: _selectedModel.isEmpty
                        ? const Color(0xFF07C160)
                        : const Color(0xFF4A90D9)),
                const SizedBox(width: 4),
                Text(
                  _selectedModel.isEmpty
                      ? '跟随路由'
                      : (_selectedModel.startsWith('free:')
                          ? '免费:${_selectedModel.split(':')[1]}'
                          : _selectedModel),
                  style: const TextStyle(color: _text, fontSize: 11),
                ),
                const Icon(Icons.arrow_drop_down, size: 16, color: _hint),
              ]),
            ),
            itemBuilder: (BuildContext context) => <PopupMenuEntry<String>>[
              const PopupMenuItem<String>(value: '', height: 36,
                  child: Text('跟随路由（本地 → 号池自动兜底）',
                      style: TextStyle(color: _text, fontSize: 12))),
              if (_wbModels.isNotEmpty) ...<PopupMenuEntry<String>>[
                const PopupMenuDivider(),
                const PopupMenuItem<String>(enabled: false, height: 28,
                    child: Text('WorkBuddy 模型',
                        style: TextStyle(color: _hint, fontSize: 10))),
                for (final m in _wbModels)
                  PopupMenuItem<String>(value: m['id'] as String, height: 34,
                      child: Text(m['name'] as String,
                          style: TextStyle(
                              color: _selectedModel == m['id']
                                  ? const Color(0xFF07C160)
                                  : _text,
                              fontSize: 12))),
              ],
              if (_freeModels.isNotEmpty) ...<PopupMenuEntry<String>>[
                const PopupMenuDivider(),
                const PopupMenuItem<String>(enabled: false, height: 28,
                    child: Text('免密免费模型',
                        style: TextStyle(color: _hint, fontSize: 10))),
                for (final m in _freeModels)
                  PopupMenuItem<String>(value: m['id'] as String, height: 34,
                      child: Text(m['name'] as String,
                          style: TextStyle(
                              color: _selectedModel == m['id']
                                  ? const Color(0xFF07C160)
                                  : _text,
                              fontSize: 12))),
              ],
            ],
          ),
        ],
      ),
      body: Column(children: <Widget>[
        Expanded(
          child: ListView(
            controller: _scroll,
            padding: const EdgeInsets.symmetric(vertical: 8),
            children: <Widget>[
              ..._messages.map(_bubble),
              if (_typing)
                Padding(
                  padding: const EdgeInsets.fromLTRB(10, 6, 10, 6),
                  child: Row(children: <Widget>[
                    _avatar(false),
                    const SizedBox(width: 8),
                    Container(
                      padding: const EdgeInsets.symmetric(
                          horizontal: 12, vertical: 10),
                      decoration: BoxDecoration(
                        color: _other,
                        borderRadius: BorderRadius.circular(8),
                      ),
                      child: const Text('大肥鱼思考中…',
                          style: TextStyle(color: _hint, fontSize: 14)),
                    ),
                  ]),
                ),
            ],
          ),
        ),
        // 表情包面板（emoji 网格：点选插入输入框）
        if (_showEmojiPanel)
          Container(
            height: 180,
            color: Colors.white,
            child: GridView.builder(
              padding: const EdgeInsets.all(8),
              gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
                  crossAxisCount: 10, mainAxisSpacing: 4, crossAxisSpacing: 4),
              itemCount: _emojiSet.length,
              itemBuilder: (BuildContext context, int index) => InkWell(
                onTap: () {
                  _input.text += _emojiSet[index];
                  _input.selection = TextSelection.fromPosition(
                      TextPosition(offset: _input.text.length));
                },
                child: Center(
                    child: Text(_emojiSet[index],
                        style: const TextStyle(fontSize: 22))),
              ),
            ),
          ),
        // 底部输入栏（微信风：白底 + 绿色发送按钮）
        Container(
          color: const Color(0xFFF7F7F7),
          padding: EdgeInsets.only(
              left: 10, right: 10, top: 6,
              bottom: 6 + MediaQuery.of(context).padding.bottom),
          child: Row(children: <Widget>[
            // 表情包开关
            IconButton(
              tooltip: '表情包',
              onPressed: () => setState(() => _showEmojiPanel = !_showEmojiPanel),
              icon: Icon(Icons.emoji_emotions_outlined,
                  size: 22,
                  color: _showEmojiPanel ? const Color(0xFF07C160) : _hint),
            ),
            Expanded(
              child: TextField(
                controller: _input,
                style: const TextStyle(color: _text, fontSize: 15),
                decoration: InputDecoration(
                  hintText: '说点什么…',
                  hintStyle: const TextStyle(color: _hint, fontSize: 14),
                  filled: true,
                  fillColor: Colors.white,
                  contentPadding:
                      const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
                  enabledBorder: OutlineInputBorder(
                      borderRadius: BorderRadius.circular(6),
                      borderSide: const BorderSide(color: Color(0xFFDDDDDD))),
                  focusedBorder: OutlineInputBorder(
                      borderRadius: BorderRadius.circular(6),
                      borderSide: const BorderSide(color: Color(0xFF07C160))),
                ),
                onSubmitted: (_) => _send(),
              ),
            ),
            const SizedBox(width: 8),
            Material(
              color: const Color(0xFF07C160),
              borderRadius: BorderRadius.circular(6),
              child: InkWell(
                borderRadius: BorderRadius.circular(6),
                onTap: _send,
                child: const Padding(
                  padding: EdgeInsets.symmetric(horizontal: 16, vertical: 10),
                  child: Text('发送',
                      style: TextStyle(color: Colors.white, fontSize: 14)),
                ),
              ),
            ),
          ]),
        ),
      ]),
    );
  }

  @override
  void dispose() {
    _framesSub?.cancel();
    _stateSub?.cancel();
    _service?.dispose();
    _input.dispose();
    _scroll.dispose();
    super.dispose();
  }
}

class _Msg {
  const _Msg({
    required this.text,
    required this.mine,
    required this.model,
    required this.kind,
    required this.fallback,
  });

  final String text;
  final bool mine;
  final String model;
  final String kind;
  final Object? fallback;
}
