import 'dart:async';
import 'dart:convert';
import 'dart:io';

/// 聊天连接状态。
enum ChatLinkState { connecting, connected, reconnecting, disconnected }

/// 聊天纯消息流客户端（独立端口 8769，区别于 8767 管理 WS）。
/// 协议：
///   首帧 → {"type":"hello","token":...,"device_id":"flutter"}
///   回   ← {"type":"hello_ack","session_id":...}
///   发   → {"type":"message","text":...}
///   回   ← typing 帧 / reply 帧（text/intent/model/provider_kind/fallback）
/// 零第三方依赖：直接用 dart:io 的 WebSocket。
class ChatService {
  ChatService({this.wsUrl = 'ws://127.0.0.1:8769'});

  final String wsUrl;

  /// 断线重连上限（指数退避：0.8s → 1.6s → 3.2s → 6.4s → 12.8s）。
  static const int _maxReconnects = 5;

  WebSocket? _socket;
  StreamSubscription<dynamic>? _sub;
  Timer? _reconnectTimer;
  bool _closed = false;
  String _token = '';
  int _attempts = 0;

  final StreamController<Map<String, dynamic>> _frames =
      StreamController<Map<String, dynamic>>.broadcast();
  final StreamController<ChatLinkState> _states =
      StreamController<ChatLinkState>.broadcast();

  /// 服务端下发的全部 JSON 帧（hello_ack / typing / reply …）。
  Stream<Map<String, dynamic>> get frames => _frames.stream;

  /// 连接状态变化（连接中 / 已连接 / 重连中 / 断开）。
  Stream<ChatLinkState> get states => _states.stream;

  bool get connected => _socket != null;

  /// 建立连接：WebSocket.connect 成功后立即发 hello 首帧（重连也会重发）。
  /// 返回本次 connect 是否成功；hello_ack 及后续帧经 [frames] 流给出。
  Future<bool> connect(String token) async {
    _token = token;
    _closed = false;
    _attempts = 0;
    _states.add(ChatLinkState.connecting);
    return _open();
  }

  Future<bool> _open() async {
    try {
      final socket =
          await WebSocket.connect(wsUrl).timeout(const Duration(seconds: 4));
      if (_closed) {
        await socket.close();
        return false;
      }
      _socket = socket;
      _attempts = 0;
      _sub = socket.listen(
        _onData,
        onDone: _handleDown,
        onError: (Object _) => _handleDown(),
      );
      socket.add(jsonEncode(<String, dynamic>{
        'type': 'hello',
        'token': _token,
        'device_id': 'flutter',
      }));
      _states.add(ChatLinkState.connected);
      return true;
    } catch (_) {
      _handleDown();
      return false;
    }
  }

  void _onData(dynamic raw) {
    if (raw is! String) return;
    try {
      final frame = jsonDecode(raw);
      if (frame is Map) {
        _frames.add(Map<String, dynamic>.from(frame));
      }
    } catch (_) {
      // 忽略非法帧
    }
  }

  /// 断线处理：指数退避重连，最多 [_maxReconnects] 次；
  /// 每次重连走 _open()，会自动重发 hello。
  void _handleDown() {
    _sub?.cancel();
    _sub = null;
    _socket = null;
    if (_closed) return;
    if (_attempts >= _maxReconnects) {
      _states.add(ChatLinkState.disconnected);
      return;
    }
    final delay = Duration(milliseconds: 800 * (1 << _attempts));
    _attempts++;
    _states.add(ChatLinkState.reconnecting);
    _reconnectTimer?.cancel();
    _reconnectTimer = Timer(delay, () {
      if (!_closed) unawaited(_open());
    });
  }

  /// 发送一条用户消息（{"type":"message","text":...}；model 非空时指定回复模型）。
  void sendMessage(String text, {String? model}) {
    _send(<String, dynamic>{
      'type': 'message', 'text': text,
      if (model != null && model.isNotEmpty) 'model': model,
    });
  }

  /// 心跳保活。
  void ping() {
    _send(<String, dynamic>{'type': 'ping'});
  }

  void _send(Map<String, dynamic> frame) {
    final socket = _socket;
    if (socket == null) return;
    try {
      socket.add(jsonEncode(frame));
    } catch (_) {
      // 发送失败交给 onDone / onError 触发重连
    }
  }

  void dispose() {
    _closed = true;
    _reconnectTimer?.cancel();
    _reconnectTimer = null;
    _sub?.cancel();
    _sub = null;
    _socket?.close();
    _socket = null;
    _frames.close();
    _states.close();
  }
}
