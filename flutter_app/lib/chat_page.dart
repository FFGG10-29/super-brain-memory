import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:file_selector/file_selector.dart';
import 'package:flutter/material.dart';

import 'brain_client.dart';
import 'chat_service.dart';
import 'prefs.dart';

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
  // 模型选择：'' = 跟随路由；其余 = WorkBuddy 模型（免密 free:* 已移除）
  String _selectedModel = '';
  bool _showEmojiPanel = false;
  // 待发送图片（data URL）：点 + 选图 → 预览 → 随消息一起发
  final List<String> _pendingImages = <String>[];

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
  bool _typing = false;
  StreamSubscription<Map<String, dynamic>>? _framesSub;
  StreamSubscription<ChatLinkState>? _stateSub;

  static const Color _bg = Color(0xFFEDEDED);
  static const Color _bar = Color(0xFFEDEDED);
  static const Color _mine = Color(0xFF95EC69);
  static const Color _other = Color(0xFFFFFFFF);
  static const Color _text = Color(0xFF191919);
  static const Color _hint = Color(0xFF9B9B9B);

  /// Prefs 键：模型选择与会话 id 跨页面/重启保留。
  static const String _kModel = 'chat.selected_model';
  static const String _kSession = 'chat.session_id';

  @override
  void initState() {
    super.initState();
    // 读回上次选择：'' = 跟随路由；若选的模型已下架，_loadModels 后仍可手动改。
    _selectedModel = Prefs.get<String>(_kModel, '');
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
      // 带上持久化的 session_id：后端 restore 同一会话，历史不丢。
      await service.connect(token,
          sessionId: Prefs.get<String>(_kSession, ''));
    } catch (error) {
      if (mounted) setState(() => _tokenError = '连接失败：$error');
    }
  }

  /// 把模型一次回复按行拆成独立气泡（大肥鱼「一句话一条」铁律）。
  /// 空行过滤；纯单行原样返回（保持空气泡也可，由调用方判断）。
  static List<String> _splitReply(String text) {
    final lines = text.split(RegExp(r'\r?\n'))
        .map((s) => s.trim())
        .where((s) => s.isNotEmpty)
        .toList();
    return lines.isEmpty ? <String>[text] : lines;
  }

  void _onFrame(Map<String, dynamic> frame) {
    final type = frame['type'];
    if (type == 'hello_ack') {
      _sessionId = (frame['session_id'] ?? '').toString();
      // 后端 create 的新会话 id 或 restore 的旧 id 都写回，形成闭环。
      if (_sessionId.isNotEmpty) Prefs.save(<String, dynamic>{_kSession: _sessionId});
      unawaited(_loadHistory());
      unawaited(_loadModels());
    } else if (type == 'typing') {
      final state = frame['state'];
      if (mounted) setState(() => _typing = state != false);
    } else if (type == 'reply') {
      if (mounted) {
        final text = (frame['text'] ?? '').toString();
        final model = (frame['model'] ?? '').toString();
        final kind = (frame['provider_kind'] ?? '').toString();
        final fallback = frame['fallback'];
        // 大肥鱼人格「一句话一条」：模型一次可回 1~3 句（换行分隔），
        // 拆成独立气泡逐条呈现（与 QQ 桥拆行发送一致）；模型标签只挂最后一条。
        final lines = _splitReply(text);
        setState(() {
          _typing = false;
          if (lines.length <= 1) {
            _messages.add(_Msg(text: text, mine: false, model: model,
                               kind: kind, fallback: fallback));
          } else {
            for (var i = 0; i < lines.length; i++) {
              final last = i == lines.length - 1;
              _messages.add(_Msg(
                  text: lines[i], mine: false,
                  model: last ? model : '', kind: last ? kind : '',
                  fallback: last ? fallback : null));
            }
          }
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

  /// 号池目录混入的非对话模型 id 特征（补全/图像/视频/语音/向量）。
  /// 聊天右上角只列 LLM：选到补全端点会直接发不出消息。
  static final RegExp _nonChatModelId = RegExp(
    r'(codewise|completion|[-_]image|[-_]video|[-_]tts|[-_]asr|[-_]embed|[-_]rerank|whisper)',
    caseSensitive: false,
  );

  bool _isChatModel(String id) {
    final base = id.toLowerCase();
    if (_nonChatModelId.hasMatch(base)) return false;
    // 纯补全底座小模型（不是对话档位）
    if (base == 'hunyuan-3b' || base.startsWith('hunyuan-7b')) return false;
    // auto / fast-model / balanced-model / deep-model 是 WorkBuddy 对话档位别名，保留
    return true;
  }

  /// 模型清单（号池目录：只取 WorkBuddy 对话模型），进页面拉一次。
  Future<void> _loadModels() async {
    try {
      final data = await widget.client.call('pool.catalog', <String, dynamic>{});
      if (!mounted || data is! Map) return;
      final disabled = ((data['disabled_models'] as List<dynamic>? ?? <dynamic>[]))
          .map((e) => e.toString())
          .toSet();
      final wb = (data['wb'] as Map? ?? {});
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
          if (row is Map
              && !disabled.contains((row['id'] ?? '').toString())
              && _isChatModel((row['id'] ?? '').toString()))
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
        // 选中模型若已下架/被禁用/变成非对话类型 → 自动回「跟随路由」并落盘，
        // 否则发消息仍带失效 id（第 324 行）导致发送失败。
        if (_selectedModel.isNotEmpty &&
            !wbRows.any((m) => m['id'] == _selectedModel)) {
          _selectedModel = '';
          Prefs.save(<String, dynamic>{_kModel: ''});
        }
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
          final mine = (row['role'] ?? '').toString() == 'user';
          final text = (row['text'] ?? '').toString();
          // 历史 assistant 多句回放同样逐条拆气泡（用户消息保持原样）
          if (!mine && text.contains(RegExp(r'\r?\n'))) {
            for (final line in _splitReply(text)) {
              _messages.add(_Msg(text: line, mine: false, model: '', kind: '', fallback: null));
            }
          } else {
            _messages.add(_Msg(text: text, mine: mine, model: '', kind: '', fallback: null));
          }
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

  /// 选图：文件选择器（png/jpg/webp/gif）→ 读字节 → data URL 进待发列表。
  Future<void> _pickImage() async {
    try {
      const group = XTypeGroup(label: '图片', extensions: <String>['png', 'jpg', 'jpeg', 'webp', 'gif']);
      final file = await openFile(acceptedTypeGroups: <XTypeGroup>[group]);
      if (file == null) return;
      final bytes = await file.readAsBytes();
      if (bytes.lengthInBytes > 8 * 1024 * 1024) {
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
              const SnackBar(content: Text('图片超过 8MB，先压缩再发'), duration: Duration(seconds: 2)));
        }
        return;
      }
      final ext = file.name.split('.').last.toLowerCase().replaceAll('jpeg', 'jpg');
      final mime = ext == 'png' ? 'image/png'
          : ext == 'webp' ? 'image/webp'
          : ext == 'gif' ? 'image/gif'
          : 'image/jpeg';
      final dataUrl = 'data:$mime;base64,${base64Encode(bytes)}';
      if (!mounted) return;
      setState(() {
        if (_pendingImages.length < 4) _pendingImages.add(dataUrl);
      });
    } catch (_) {
      // 选图失败不打断聊天
    }
  }

  void _send() {
    final text = _input.text.trim();
    final images = List<String>.from(_pendingImages);
    if ((text.isEmpty && images.isEmpty) || _service == null) return;
    _service!.sendMessage(text,
        model: _selectedModel.isEmpty ? null : _selectedModel,
        images: images.isEmpty ? null : images);
    setState(() {
      _messages.add(_Msg(text: text, mine: true, model: '', kind: '',
          fallback: null, images: images.isEmpty ? null : images));
      _typing = true;
      _input.clear();
      _pendingImages.clear();
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
                if (msg.images != null && msg.images!.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 4),
                    child: Column(children: <Widget>[
                      for (final src in msg.images!) _imageThumb(src),
                    ]),
                  ),
                if (msg.text.isNotEmpty)
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

  /// data URL → 气泡缩略图（宽 ≤200，圆角）；非 data URL（http 图）直接 Image.network。
  static Widget _imageThumb(String src) {
    const double w = 200;
    if (src.startsWith('data:image/')) {
      final b64 = src.split(',').last;
      try {
        final Uint8List bytes = base64Decode(b64);
        return ClipRRect(
          borderRadius: BorderRadius.circular(6),
          child: Image.memory(bytes, width: w, fit: BoxFit.cover),
        );
      } catch (_) {
        return const Text('（图片解码失败）', style: TextStyle(color: _hint, fontSize: 12));
      }
    }
    return ClipRRect(
      borderRadius: BorderRadius.circular(6),
      child: Image.network(src, width: w, fit: BoxFit.cover, errorBuilder:
          (_, __, ___) => const Text('（图片加载失败）',
              style: TextStyle(color: _hint, fontSize: 12))),
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
            onSelected: (value) {
              setState(() => _selectedModel = value);
              Prefs.save(<String, dynamic>{_kModel: value});   // 选了就记住，关页面不丢
            },
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
                  _selectedModel.isEmpty ? '跟随路由' : _selectedModel,
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
          child: Column(mainAxisSize: MainAxisSize.min, children: <Widget>[
            // 待发图片预览条
            if (_pendingImages.isNotEmpty)
              Container(
                margin: const EdgeInsets.only(bottom: 6),
                height: 64,
                child: Row(children: <Widget>[
                  for (final src in _pendingImages)
                    Stack(children: <Widget>[
                      Padding(
                        padding: const EdgeInsets.only(right: 6),
                        child: ClipRRect(
                          borderRadius: BorderRadius.circular(6),
                          child: _imageThumb(src),
                        ),
                      ),
                      Positioned(
                        top: 0, right: 0,
                        child: GestureDetector(
                          onTap: () => setState(() => _pendingImages.remove(src)),
                          child: Container(
                            padding: const EdgeInsets.all(2),
                            decoration: const BoxDecoration(
                                color: Colors.black54, shape: BoxShape.circle),
                            child: const Icon(Icons.close, size: 12, color: Colors.white),
                          ),
                        ),
                      ),
                    ]),
                ]),
              ),
            Row(children: <Widget>[
              // 表情包开关
              IconButton(
                tooltip: '表情包',
                onPressed: () => setState(() => _showEmojiPanel = !_showEmojiPanel),
                icon: Icon(Icons.emoji_emotions_outlined,
                    size: 22,
                    color: _showEmojiPanel ? const Color(0xFF07C160) : _hint),
              ),
              // 发图（号池多模态模型可识别）
              IconButton(
                tooltip: '发图片',
                onPressed: _pendingImages.length >= 4 ? null : _pickImage,
                icon: const Icon(Icons.add_photo_alternate_outlined,
                    size: 22, color: _hint),
              ),
              Expanded(
                child: TextField(
                  controller: _input,
                  style: const TextStyle(color: _text, fontSize: 15),
                  decoration: InputDecoration(
                    hintText: _pendingImages.isEmpty ? '说点什么…' : '图片已就绪，可配一句话…',
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
    this.images,
  });

  final String text;
  final bool mine;
  final String model;
  final String kind;
  final Object? fallback;
  final List<String>? images;
}
