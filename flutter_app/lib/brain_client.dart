import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'graph_model.dart';

/// v3 Daemon 客户端：**所有功能页统一走 WebSocket RPC**，HTTP 仅作断线兜底。
/// 协议 → {"type":"rpc","id","method","args"}  ← {"type":"rpc_result","id","ok","result"}
/// 零第三方依赖（dart:io WebSocket / HttpClient）。
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
  int _requestId = 0;
  final _pending = <String, Completer<dynamic>>{};

  final StreamController<Map<String, dynamic>> _events =
      StreamController<Map<String, dynamic>>.broadcast();

  Stream<Map<String, dynamic>> get events => _events.stream;
  bool get connected => _socket != null;

  // ── WS RPC ────────────────────────────────────────────────────────────
  Future<dynamic> call(String method, [Map<String, dynamic> args = const <String, dynamic>{}]) async {
    final socket = _socket;
    if (socket == null) return _httpFallback(method, args);
    final id = (++_requestId).toString();
    final completer = Completer<dynamic>();
    _pending[id] = completer;
    try {
      socket.add(jsonEncode(<String, dynamic>{
        'type': 'rpc', 'id': id, 'method': method, 'args': args,
      }));
      return await completer.future.timeout(
        const Duration(seconds: 12),
        onTimeout: () {
          _pending.remove(id);
          return _httpFallback(method, args);
        },
      );
    } catch (_) {
      _pending.remove(id);
      return _httpFallback(method, args);
    }
  }

  void _onFrame(String raw) {
    try {
      final message = jsonDecode(raw) as Map<String, dynamic>;
      final type = message['type']?.toString() ?? '';
      if (type == 'rpc_result') {
        final id = message['id']?.toString() ?? '';
        final completer = _pending.remove(id);
        if (completer != null && !completer.isCompleted) {
          completer.complete(message['ok'] == true
              ? message['result']
              : <String, dynamic>{'error': message['error']?.toString() ?? 'RPC 失败'});
        }
      } else {
        _events.add(message);
      }
    } catch (_) {
      // 忽略非法帧
    }
  }

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
        (raw) => _onFrame(raw as String),
        onDone: _scheduleReconnect,
        onError: (_) => _scheduleReconnect(),
      );
    }).catchError((_) => _scheduleReconnect());
  }

  void _scheduleReconnect() {
    _socket = null;
    for (final completer in _pending.values) {
      if (!completer.isCompleted) completer.complete(<String, dynamic>{'error': 'WS 断开'});
    }
    _pending.clear();
    if (_closed) return;
    _reconnectTimer?.cancel();
    _reconnectTimer = Timer(const Duration(milliseconds: 1500), () {
      _events.add(<String, dynamic>{'type': 'reconnecting'});
      connectEvents();
    });
  }

  // ── 业务方法（全部经 RPC）─────────────────────────────────────────────
  Future<BrainGraph> graph() async {
    final data = await call('graph');
    return BrainGraph.fromJson(_asMap(data));
  }

  Future<Map<String, dynamic>> health() async => _asMap(await call('health'));
  Future<Map<String, dynamic>> config() async => _asMap(await call('config.get'));
  Future<Map<String, dynamic>> snapshot() async => _asMap(await call('snapshot'));
  Future<Map<String, dynamic>> spaces() async => _asMap(await call('spaces'));
  Future<Map<String, dynamic>> engines() async => _asMap(await call('engines'));
  Future<Map<String, dynamic>> modelStatus() async => _asMap(await call('model_status'));
  Future<Map<String, dynamic>> gateway() async => _asMap(await call('gateway'));

  Future<List<Map<String, dynamic>>> tasks() async {
    final data = await call('tasks');
    return (data as List<dynamic>? ?? <dynamic>[])
        .map((row) => _asMap(row))
        .toList();
  }

  Future<Map<String, dynamic>> runTask(String name) async =>
      _asMap(await call('run_task', <String, dynamic>{'name': name}));

  Future<Map<String, dynamic>> syncOnce() async => _asMap(await call('sync_once'));
  Future<Map<String, dynamic>> createSnapshot() async => _asMap(await call('create_snapshot'));
  Future<Map<String, dynamic>> exportAll() async => _asMap(await call('export'));
  Future<Map<String, dynamic>> restartKernel() async => _asMap(await call('restart'));

  Future<List<Map<String, dynamic>>> snapshots() async {
    final data = await call('snapshots');
    return (data as List<dynamic>? ?? <dynamic>[]).map((row) => _asMap(row)).toList();
  }

  Future<Map<String, dynamic>> detail(String id) async =>
      _asMap(await call('detail', <String, dynamic>{'memory_id': id}));

  Future<Map<String, dynamic>> setConfig(Map<String, dynamic> patches) async =>
      _asMap(await call('config.set', <String, dynamic>{'patches': patches}));

  Future<Map<String, dynamic>> setClientConfig(Map<String, dynamic> nebula) async =>
      _asMap(await call('client_config.set', <String, dynamic>{'nebula': nebula}));

  Future<List<RecallHit>> recall(String query, {int limit = 8}) async {
    if (query.trim().isEmpty) return <RecallHit>[];
    final data = _asMap(await call('recall', <String, dynamic>{'q': query, 'limit': limit}));
    return (data['hits'] as List<dynamic>? ?? <dynamic>[])
        .map((row) => RecallHit.fromJson(_asMap(row)))
        .toList();
  }

  Future<List<MemoryRow>> memories({int limit = 200}) async {
    final data = await call('memories', <String, dynamic>{'limit': limit});
    // WS 直接返回数组；HTTP 兜底返回 {"memories":[...]}
    final dynamic rows = data is List
        ? data
        : (data is Map ? (data['memories'] ?? data['result'] ?? <dynamic>[]) : <dynamic>[]);
    return (rows as List<dynamic>).map((row) => MemoryRow.fromJson(_asMap(row))).toList();
  }

  Future<Map<String, dynamic>> saveMemory(String content, {String agentId = 'flutter'}) async =>
      _asMap(await call('save_memory', <String, dynamic>{'content': content, 'agent_id': agentId}));

  Future<Map<String, dynamic>> saveConfig(Map<String, dynamic> patch) async {
    final nebula = patch['nebula'] as Map<String, dynamic>? ?? patch;
    return setClientConfig(nebula);
  }

  // ── 功能补齐：检查器 / 用户控制 / 时间轴 / 导入 / 全量配置 ──────────
  Future<List<Map<String, dynamic>>> memoryList({int limit = 200, String keyword = '',
      String tier = '', String memoryType = '', String namespace = ''}) async {
    final data = await call('memory.list', <String, dynamic>{
      'limit': limit, 'keyword': keyword, 'tier': tier,
      'memory_type': memoryType, 'namespace': namespace,
    });
    final rows = data is List<dynamic> ? data : <dynamic>[];
    return rows.map((row) => _asMap(row)).toList();
  }

  Future<Map<String, dynamic>> inspectMemory(String id) async =>
      _asMap(await call('memory.inspect', <String, dynamic>{'memory_id': id}));

  Future<Map<String, dynamic>> correctMemory(String id, Map<String, dynamic> changes,
          {String reason = ''}) async =>
      _asMap(await call('memory.correct', <String, dynamic>{
        'memory_id': id, 'changes': changes, 'reason': reason,
      }));

  Future<Map<String, dynamic>> forgetMemory(String id, {String reason = '', String action = 'forget'}) async =>
      _asMap(await call('memory.forget', <String, dynamic>{
        'memory_id': id, 'reason': reason, 'action': action,
      }));

  Future<List<Map<String, dynamic>>> memoryAudit(String id, {int limit = 50}) async {
    final data = await call('memory.audit', <String, dynamic>{'memory_id': id, 'limit': limit});
    final rows = data is List<dynamic> ? data : <dynamic>[];
    return rows.map((row) => _asMap(row)).toList();
  }

  Future<Map<String, dynamic>> setSensitive(String id, bool flag) async =>
      _asMap(await call('memory.sensitive', <String, dynamic>{'memory_id': id, 'flag': flag}));

  Future<Map<String, dynamic>> coreMemory() async => _asMap(await call('core_memory.get'));

  Future<Map<String, dynamic>> saveCoreMemory(Map<String, dynamic> blocks) async =>
      _asMap(await call('core_memory.set', <String, dynamic>{'blocks': blocks}));

  Future<List<Map<String, dynamic>>> timeline({List<String> types = const <String>[], int limit = 200}) async {
    final data = await call('timeline', <String, dynamic>{'types': types, 'limit': limit});
    final rows = data is List<dynamic> ? data : <dynamic>[];
    return rows.map((row) => _asMap(row)).toList();
  }

  Future<Map<String, dynamic>> configPresets() async => _asMap(await call('config.presets'));
  Future<Map<String, dynamic>> applyPreset(String name) async =>
      _asMap(await call('config.apply_preset', <String, dynamic>{'name': name}));
  Future<Map<String, dynamic>> resetConfig() async => _asMap(await call('config.reset'));
  Future<Map<String, dynamic>> gatewayStatus() async => _asMap(await call('gateway.status'));

  Future<Map<String, dynamic>> importMemories(String text, String suffix,
          {bool fast = true}) async =>
      _asMap(await call('import_memories', <String, dynamic>{
        'text': text, 'suffix': suffix, 'fast': fast,
      }));

  // ── HTTP 兜底（WS 未连通时）──────────────────────────────────────────
  Future<dynamic> _httpFallback(String method, Map<String, dynamic> args) async {
    try {
      switch (method) {
        case 'graph':
          return await _get('/v1/graph');
        case 'health':
          return await _get('/v1/health');
        case 'recall':
          return await _get('/v1/recall?q=${Uri.encodeComponent(args['q']?.toString() ?? '')}&limit=${args['limit'] ?? 8}');
        case 'memories':
          return await _get('/v1/memories?limit=${args['limit'] ?? 200}');
        case 'config.get':
          return <String, dynamic>{'sections': <String, dynamic>{}, 'client': await _get('/v1/config')};
        case 'save_memory':
          return await _post('/v1/events', <String, dynamic>{
            'content': args['content'], 'agent_id': args['agent_id'] ?? 'flutter',
          });
        case 'client_config.set':
          return await _post('/v1/config', <String, dynamic>{'nebula': args['nebula'] ?? <String, dynamic>{}});
        default:
          return <String, dynamic>{'error': 'WebSocket 未连接：$method 需要 v3 Daemon WS'};
      }
    } catch (error) {
      return <String, dynamic>{'error': '$error'};
    }
  }

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

  static Map<String, dynamic> _asMap(dynamic value) =>
      value is Map<String, dynamic> ? value : <String, dynamic>{'error': '返回格式异常'};

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
