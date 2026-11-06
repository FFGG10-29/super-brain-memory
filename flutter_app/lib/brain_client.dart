import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'graph_model.dart';

/// v3 Daemon 客户端：HTTP（快照/召回/受理/配置）+ WebSocket（增量事件）。
/// 只用 dart:io，零第三方依赖，Windows 桌面可直接编译。
class BrainClient {
  BrainClient({required this.host, this.httpPort = 8768, this.wsPort = 8767});

  final String host;
  final int httpPort;
  final int wsPort;

  String get base => 'http://$host:$httpPort';
  String get wsUrl => 'ws://$host:$wsPort/ws';

  final _http = HttpClient()..connectionTimeout = const Duration(seconds: 4);
  WebSocket? _socket;
  Timer? _reconnectTimer;
  bool _closed = false;

  final StreamController<Map<String, dynamic>> _events =
      StreamController<Map<String, dynamic>>.broadcast();

  Stream<Map<String, dynamic>> get events => _events.stream;
  bool get connected => _socket != null;

  Future<Map<String, dynamic>> _get(String path) async {
    final request = await _http.getUrl(Uri.parse('$base$path'));
    final response = await request.close();
    final body = await response.transform(utf8.decoder).join();
    return jsonDecode(body) as Map<String, dynamic>;
  }

  Future<Map<String, dynamic>> _post(String path, Map<String, dynamic> payload) async {
    final request = await _http.postUrl(Uri.parse('$base$path'));
    request.headers.contentType = ContentType.json;
    request.add(utf8.encode(jsonEncode(payload)));
    final response = await request.close();
    final body = await response.transform(utf8.decoder).join();
    return jsonDecode(body) as Map<String, dynamic>;
  }

  Future<BrainGraph> graph() async => BrainGraph.fromJson(await _get('/v1/graph'));
  Future<Map<String, dynamic>> health() async => _get('/v1/health');
  Future<Map<String, dynamic>> config() async => _get('/v1/config');

  Future<List<RecallHit>> recall(String query, {int limit = 8}) async {
    if (query.trim().isEmpty) return <RecallHit>[];
    final data = await _get('/v1/recall?q=${Uri.encodeComponent(query)}&limit=$limit');
    return (data['hits'] as List<dynamic>? ?? <dynamic>[])
        .map((row) => RecallHit.fromJson(row as Map<String, dynamic>))
        .toList();
  }

  Future<List<MemoryRow>> memories({int limit = 200}) async {
    final data = await _get('/v1/memories?limit=$limit');
    return (data['memories'] as List<dynamic>? ?? <dynamic>[])
        .map((row) => MemoryRow.fromJson(row as Map<String, dynamic>))
        .toList();
  }

  Future<Map<String, dynamic>> saveMemory(String content, {String agentId = 'flutter'}) =>
      _post('/v1/events', <String, dynamic>{'content': content, 'agent_id': agentId});

  Future<Map<String, dynamic>> saveConfig(Map<String, dynamic> patch) =>
      _post('/v1/config', patch);

  /// 连接 WS；断线自动 1.5s 重连。事件流由 [events] 广播。
  void connectEvents() {
    if (_closed || _socket != null) return;
    WebSocket.connect(wsUrl).then((socket) {
      if (_closed) {
        socket.close();
        return;
      }
      _socket = socket;
      _events.add(<String, dynamic>{'type': 'connected'});
      socket.listen(
        (raw) {
          try {
            _events.add(jsonDecode(raw as String) as Map<String, dynamic>);
          } catch (_) {
            // 忽略非法帧
          }
        },
        onDone: _scheduleReconnect,
        onError: (_) => _scheduleReconnect(),
      );
    }).catchError((_) => _scheduleReconnect());
  }

  void _scheduleReconnect() {
    _socket = null;
    if (_closed) return;
    _reconnectTimer?.cancel();
    _reconnectTimer = Timer(const Duration(milliseconds: 1500), () {
      _events.add(<String, dynamic>{'type': 'reconnecting'});
      connectEvents();
    });
  }

  void dispose() {
    _closed = true;
    _reconnectTimer?.cancel();
    _socket?.close();
    _events.close();
    _http.close(force: true);
  }
}

class RecallHit {
  RecallHit({required this.id, required this.content, required this.score, this.memoryType = 'fact'});
  final String id;
  final String content;
  final double score;
  final String memoryType;

  factory RecallHit.fromJson(Map<String, dynamic> json) => RecallHit(
        id: json['id']?.toString() ?? '',
        content: json['content']?.toString() ?? '',
        score: (json['score'] as num? ?? 0).toDouble(),
        memoryType: json['memory_type']?.toString() ?? 'fact',
      );
}

class MemoryRow {
  MemoryRow({required this.id, required this.content, this.memoryType = 'fact', this.tier = 'buffer', this.pad = const <double>[0, 0, 0]});
  final String id;
  final String content;
  final String memoryType;
  final String tier;
  final List<double> pad;

  factory MemoryRow.fromJson(Map<String, dynamic> json) => MemoryRow(
        id: json['id']?.toString() ?? '',
        content: json['content']?.toString() ?? '',
        memoryType: json['memory_type']?.toString() ?? 'fact',
        tier: json['tier']?.toString() ?? 'buffer',
        pad: (json['pad'] as List<dynamic>? ?? <dynamic>[0, 0, 0])
            .map((value) => (value as num? ?? 0).toDouble())
            .toList(),
      );
}
