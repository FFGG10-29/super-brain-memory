import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';

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
  Future<void>? _connecting;
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
    var socket = _socket;
    if (socket == null) {
      // 各页面 initState 往往早于 WS 握手完成；先等连接就绪，
      // 否则首屏全部误走 HTTP 兜底且不会自动重试（旧版显示「WebSocket 未连接」）。
      await _awaitConnection();
      socket = _socket;
    }
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

  /// 等待 WS 就绪（最多 3 秒）。连接失败时返回，由 HTTP /v1/rpc 兜底接管。
  Future<void> _awaitConnection() async {
    if (_socket != null || _closed) return;
    connectEvents();
    for (var attempt = 0; attempt < 30; attempt++) {
      await Future<void>.delayed(const Duration(milliseconds: 100));
      if (_socket != null || _closed) return;
    }
  }

  void connectEvents() {
    // 首屏多个页面会同时调 call() → _awaitConnection()；没有这个闸门会
    // 并发发起多条 WebSocket 连接（旧版 netstat 出现重复 ESTABLISHED）。
    if (_closed || _socket != null || _connecting != null) return;
    _connecting = WebSocket.connect(wsUrl).then((socket) {
      _connecting = null;
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
    }).catchError((Object _) {
      _connecting = null;
      _scheduleReconnect();
    });
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
  /// 首屏取图：后端可能比 UI 晚 1~3 秒才监听端口，第一次调用必然失败。
  /// 这里带重试，且把「返回里没有 nodes」当成失败而不是静默的空图
  /// （旧版就是这么把 0 节点画到屏幕上的：错误被当成空数组吞掉）。
  Future<BrainGraph> graph({int attempts = 3}) async {
    Map<String, dynamic> last = <String, dynamic>{};
    for (var i = 0; i < attempts; i++) {
      final data = _asMap(await call('graph'));
      if (data['error'] == null && data['nodes'] is List) {
        final rawNodes = (data['nodes'] as List<dynamic>).length;
        // 星团标签：把 topic:<id> 映射成空间名（后端不可用时静默降级为 id）
        Map<String, String> topicNames = <String, String>{};
        try {
          final tl = _asMap(await call('topics.list').timeout(
              const Duration(seconds: 2),
              onTimeout: () => <String, dynamic>{}));
          for (final raw in _asList(tl['spaces'])) {
            final s = _asMap(raw);
            final id = (s['id'] ?? '').toString();
            if (id.isNotEmpty) topicNames[id] = (s['name'] ?? id).toString();
          }
        } catch (_) {}
        final built = BrainGraph.fromJson(data, topicNames: topicNames);
        debugPrint('[nebula] graph ok: raw=$rawNodes parsed=${built.nodes.length} '
            'edges=${built.edges.length} clusters=${built.clusters.length}');
        return built;
      }
      last = data;
      lastGraphError = '${data['error'] ?? '返回无 nodes 字段'}';
      debugPrint('[nebula] graph 第 ${i + 1} 次失败：$lastGraphError');
      if (i + 1 < attempts) await Future<void>.delayed(const Duration(milliseconds: 700));
    }
    lastGraphError = last['error']?.toString() ?? '后端返回空节点列表';
    return BrainGraph(
      nodes: <BrainNode>[],
      edges: <BrainEdge>[],
      clusters: <GalaxyCluster>[],
    );
  }

  /// 上一次 graph() 失败的原因（供星云页把错误显示出来，而不是 0 节点）。
  String lastGraphError = '';

  /// 全量渲染：优先走后端分批协议（graph.chunk，每批 500 个节点，零边计算）；
  /// 当前冻结后端尚无该 RPC 时，降级为一次 `memories(limit=100000)` 取全量，
  /// 由 NebulaPage 按 500 一批渐进渲染 —— 两条路径都能边收边出图。
  Future<List<Map<String, dynamic>>> allMemories() async {
    final rows = <Map<String, dynamic>>[];
    try {
      final probe = _asMap(await call('graph.chunk',
          <String, dynamic>{'offset': 0, 'limit': 500}).timeout(
        const Duration(seconds: 3),
        onTimeout: () => <String, dynamic>{},
      ));
      if (probe['total'] != null && probe['nodes'] is List) {
        final total = (probe['total'] as num).toInt();
        for (final raw in probe['nodes'] as List<dynamic>) {
          rows.add(_asMap(raw));
        }
        var offset = rows.length;
        while (offset < total) {
          final chunk = _asMap(await call('graph.chunk',
              <String, dynamic>{'offset': offset, 'limit': 500}));
          final part = chunk['nodes'] as List<dynamic>? ?? <dynamic>[];
          if (part.isEmpty) break;
          for (final raw in part) {
            rows.add(_asMap(raw));
          }
          offset += part.length;
        }
        return rows;
      }
    } catch (_) {
      // 新协议不可用 → 走兼容路径
    }
    final data = await call('memories', <String, dynamic>{'limit': 100000});
    final list = data is List<dynamic>
        ? data
        : (data is Map<String, dynamic>
            ? (data['memories'] ?? data['result'] ?? data['items'])
            : null);
    if (list is List<dynamic>) {
      for (final raw in list) {
        rows.add(_asMap(raw));
      }
    }
    debugPrint('[nebula] allMemories 兼容路径取到 ${rows.length} 行');
    return rows;
  }

  Future<Map<String, dynamic>> health() async => _asMap(await call('health'));
  Future<Map<String, dynamic>> config() async => _asMap(await call('config.get'));
  Future<Map<String, dynamic>> snapshot() async => _asMap(await call('snapshot'));
  Future<Map<String, dynamic>> spaces() async => _asMap(await call('spaces'));
  Future<Map<String, dynamic>> engines() async => _asMap(await call('engines'));

  /// 系统概览增强：六引擎实时计数 / 投影足迹 / 空间绑定 / 召回通道 / 清洗记录。
  Future<Map<String, dynamic>> storageOverview() async =>
      _asMap(await call('storage.overview'));

  // ── L0 人格层：可视化 + 手动微调 + 反馈演化 ──────────────────────────
  Future<Map<String, dynamic>> personaGet() async => _asMap(await call('persona.get'));
  Future<Map<String, dynamic>> personaStatus() async => _asMap(await call('persona.status'));
  Future<String> personaBaseline() async {
    final d = _asMap(await call('persona.baseline'));
    return (d['content'] ?? '').toString();
  }
  Future<Map<String, dynamic>> personaSetParams(Map<String, dynamic> patch,
          {String reason = '前端手动微调'}) async =>
      _asMap(await call('persona.set_params', <String, dynamic>{
        'patch': patch, 'actor': 'flutter', 'reason': reason,
      }));
  Future<Map<String, dynamic>> personaRollback(int version) async =>
      _asMap(await call('persona.rollback', <String, dynamic>{'version': version}));
  Future<Map<String, dynamic>> personaProposals() async =>
      _asMap(await call('persona.proposals'));
  Future<Map<String, dynamic>> personaApplyProposal(String id) async =>
      _asMap(await call('persona.apply_proposal', <String, dynamic>{'id': id}));
  Future<Map<String, dynamic>> personaFeedback({
    required String signal,
    required double valence,
    String target = 'sassiness',
    String sessionId = 'flutter',
  }) async =>
      _asMap(await call('persona.feedback', <String, dynamic>{
        'signal': signal, 'valence': valence, 'target': target,
        'session_id': sessionId, 'actor': 'flutter',
      }));

  // ── 主题分区 / 真实关联边 ────────────────────────────────────────────
  Future<Map<String, dynamic>> topicsList() async => _asMap(await call('topics.list'));
  Future<Map<String, dynamic>> topicsMigrate() async => _asMap(await call('topics.migrate'));
  Future<Map<String, dynamic>> realEdges({int limit = 500}) async =>
      _asMap(await call('graph.real_edges', <String, dynamic>{'limit': limit}));
  Future<Map<String, dynamic>> modelStatus() async => _asMap(await call('model_status'));
  Future<Map<String, dynamic>> gateway() async => _asMap(await call('gateway'));

  Future<List<Map<String, dynamic>>> tasks() async {
    final data = await call('tasks');
    return _asList(data).map((row) => _asMap(row)).toList();
  }

  Future<Map<String, dynamic>> runTask(String name) async =>
      _asMap(await call('run_task', <String, dynamic>{'name': name}));

  Future<Map<String, dynamic>> syncOnce() async => _asMap(await call('sync_once'));
  Future<Map<String, dynamic>> createSnapshot() async => _asMap(await call('create_snapshot'));
  Future<Map<String, dynamic>> exportAll() async => _asMap(await call('export'));
  Future<Map<String, dynamic>> restartKernel() async => _asMap(await call('restart'));

  Future<List<Map<String, dynamic>>> snapshots() async {
    final data = await call('snapshots');
    return _asList(data).map((row) => _asMap(row)).toList();
  }

  Future<Map<String, dynamic>> detail(String id) async =>
      _asMap(await call('detail', <String, dynamic>{'memory_id': id}));

  Future<Map<String, dynamic>> setConfig(Map<String, dynamic> patches) async =>
      _asMap(await call('config.set', <String, dynamic>{'patches': patches}));

  Future<Map<String, dynamic>> setClientConfig(Map<String, dynamic> nebula) async =>
      _asMap(await call('client_config.set', <String, dynamic>{'nebula': nebula}));

  /// 完整召回结果：hits + audit（六通道计数/权重/相似度）+ injection + degraded。
  /// deep=false 走只读投影的 2-gram 召回（毫秒级）；deep=true 才走六通道精排，
  /// 仅「召回归因」页使用——后者在模型加载/重排时可能挂起。
  Future<Map<String, dynamic>> recallDetailed(String query,
      {int limit = 8, bool deep = false}) async {
    if (query.trim().isEmpty) {
      return <String, dynamic>{'query': query, 'hits': <dynamic>[], 'audit': <String, dynamic>{}};
    }
    return _asMap(await call('recall', <String, dynamic>{
      'q': query, 'limit': limit, 'deep': deep,
    }));
  }

  // ── 分层上下文（dsh-mnemon 三层 View + 技能层）──────────────────────
  Future<Map<String, dynamic>> contextStatus() async =>
      _asMap(await call('context.status'));

  Future<Map<String, dynamic>> contextCompose(String query) async =>
      _asMap(await call('context.compose', <String, dynamic>{'query': query}));

  Future<Map<String, dynamic>> runtimeGet() async =>
      _asMap(await call('runtime.get'));

  Future<Map<String, dynamic>> runtimeMutate(String action, String target,
      {String content = '', String oldText = '', String importance = 'normal'}) async =>
      _asMap(await call('runtime.mutate', <String, dynamic>{
        'action': action, 'target': target, 'content': content,
        'old_text': oldText, 'importance': importance,
      }));

  Future<List<dynamic>> documentsList({String keyword = '', bool includeArchived = false}) async {
    final result = await call('documents.list', <String, dynamic>{
      'keyword': keyword, 'include_archived': includeArchived,
    });
    return result is List ? result : <dynamic>[];
  }

  Future<Map<String, dynamic>> documentsRead(String id) async =>
      _asMap(await call('documents.read', <String, dynamic>{'id': id}));

  Future<Map<String, dynamic>> documentsMutate(String action,
      {String id = '', String title = '', String description = '', String content = ''}) async =>
      _asMap(await call('documents.mutate', <String, dynamic>{
        'action': action, 'id': id, 'title': title,
        'description': description, 'content': content,
      }));

  Future<Map<String, dynamic>> spacesList() async =>
      _asMap(await call('spaces.list'));

  Future<Map<String, dynamic>> spacesCreate(String name, {String provider = 'super-brain-native'}) async =>
      _asMap(await call('spaces.create', <String, dynamic>{'name': name, 'provider': provider}));

  Future<Map<String, dynamic>> spacesActivate(String id, bool active) async =>
      _asMap(await call('spaces.activate', <String, dynamic>{'id': id, 'active': active}));

  Future<Map<String, dynamic>> spacesDelete(String id, {String migrateTo = 'default'}) async =>
      _asMap(await call('spaces.delete', <String, dynamic>{'id': id, 'migrate_to': migrateTo}));

  Future<Map<String, dynamic>> skillsList() async =>
      _asMap(await call('skills.list'));

  Future<Map<String, dynamic>> skillsGet(String id) async =>
      _asMap(await call('skills.get', <String, dynamic>{'id': id}));

  Future<Map<String, dynamic>> skillsUpsert(
      {String id = '', String name = '', String description = '', String content = ''}) async =>
      _asMap(await call('skills.upsert', <String, dynamic>{
        'id': id, 'name': name, 'description': description, 'content': content,
      }));

  Future<Map<String, dynamic>> skillsEvolve(String id, bool success, {String note = ''}) async =>
      _asMap(await call('skills.evolve', <String, dynamic>{
        'id': id, 'success': success, 'note': note,
      }));

  Future<Map<String, dynamic>> skillsDelete(String id) async =>
      _asMap(await call('skills.delete', <String, dynamic>{'id': id}));

  /// 托盘「完全退出」：通知后端优雅停机（flush journal / WAL TRUNCATE / 关六引擎）。
  /// 后端先回 RPC 再在 1 秒后停，这里只等 2 秒，不拖住前端退出。
  Future<void> shutdownBackend() async {
    try {
      await call('system.shutdown').timeout(const Duration(seconds: 2));
    } catch (_) {
      // 后端可能已经停了；交给 BackendLauncher.stopOwned() 按 PID 兜底
    }
  }

  Future<List<RecallHit>> recall(String query, {int limit = 8, bool deep = false}) async {
    final data = await recallDetailed(query, limit: limit, deep: deep);
    return _asList(data['hits'])
        .map((row) => RecallHit.fromJson(_asMap(row)))
        .toList();
  }

  // ── 记忆全量缓存 ──────────────────────────────────────────────────────
  // 首次拉全量（慢可接受，类似星云图首屏）；之后只增量拉 updated_at_ms 大于已缓存
  // 最大值的记忆并合并。列表永远从缓存读，按时间戳最新在前。遗忘/封存靠 full
  // 同步对账清除（主动操作后走 full）。
  final Map<String, Map<String, dynamic>> _memCache = <String, Map<String, dynamic>>{};
  int _memMaxTs = 0;
  bool _memReady = false;

  int _tsOf(Map<String, dynamic> row) {
    final v = row['updated_at_ms'];
    if (v is num) return v.toInt();
    final id = row['id']?.toString() ?? '';
    final hex = id.replaceAll('-', '');
    if (hex.length >= 12) {
      final ms = int.tryParse(hex.substring(0, 12), radix: 16);
      if (ms != null) return ms;
    }
    return 0;
  }

  void _absorbMem(Map<String, dynamic> row) {
    final id = row['id']?.toString() ?? '';
    if (id.isEmpty) return;
    _memCache[id] = row;
    final ts = _tsOf(row);
    if (ts > _memMaxTs) _memMaxTs = ts;
  }

  Future<void> syncMemories({bool full = false}) async {
    if (full || !_memReady) {
      final rows = await memoryList(limit: 0);
      _memCache.clear();
      _memMaxTs = 0;
      for (final row in rows) _absorbMem(row);
      _memReady = true;
    } else if (_memMaxTs > 0) {
      final rows = await memoryList(limit: 0, sinceMs: _memMaxTs);
      for (final row in rows) _absorbMem(row);
    }
  }

  List<Map<String, dynamic>> get memCacheView {
    final list = _memCache.values.toList();
    list.sort((a, b) => _tsOf(b).compareTo(_tsOf(a))); // 最新在前
    return list;
  }

  Future<List<MemoryRow>> memories({bool full = false}) async {
    await syncMemories(full: full);
    return memCacheView.map((row) => MemoryRow.fromJson(row)).toList();
  }

  Future<Map<String, dynamic>> saveMemory(String content, {String agentId = 'flutter'}) async =>
      _asMap(await call('save_memory', <String, dynamic>{'content': content, 'agent_id': agentId}));

  Future<Map<String, dynamic>> saveConfig(Map<String, dynamic> patch) async {
    final nebula = patch['nebula'] as Map<String, dynamic>? ?? patch;
    return setClientConfig(nebula);
  }

  // ── 功能补齐：检查器 / 用户控制 / 时间轴 / 导入 / 全量配置 ──────────
  Future<List<Map<String, dynamic>>> memoryList({int limit = 0, String keyword = '',
      String tier = '', String memoryType = '', String namespace = '', int sinceMs = 0}) async {
    final args = <String, dynamic>{
      'limit': limit, 'keyword': keyword, 'tier': tier,
      'memory_type': memoryType, 'namespace': namespace,
    };
    if (sinceMs > 0) args['since_ms'] = sinceMs;
    final data = await call('memory.list', args);
    final rows = _asList(data);
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
    final rows = _asList(data);
    return rows.map((row) => _asMap(row)).toList();
  }

  Future<Map<String, dynamic>> setSensitive(String id, bool flag) async =>
      _asMap(await call('memory.sensitive', <String, dynamic>{'memory_id': id, 'flag': flag}));

  Future<Map<String, dynamic>> coreMemory() async => _asMap(await call('core_memory.get'));

  Future<Map<String, dynamic>> saveCoreMemory(Map<String, dynamic> blocks) async =>
      _asMap(await call('core_memory.set', <String, dynamic>{'blocks': blocks}));

  Future<List<Map<String, dynamic>>> timeline({List<String> types = const <String>[], int limit = 200}) async {
    final data = await call('timeline', <String, dynamic>{'types': types, 'limit': limit});
    final rows = _asList(data);
    return rows.map((row) => _asMap(row)).toList();
  }

  Future<Map<String, dynamic>> configPresets() async => _asMap(await call('config.presets'));
  Future<Map<String, dynamic>> applyPreset(String name) async =>
      _asMap(await call('config.apply_preset', <String, dynamic>{'name': name}));
  Future<Map<String, dynamic>> resetConfig() async => _asMap(await call('config.reset'));
  Future<Map<String, dynamic>> gatewayStatus() async => _asMap(await call('gateway.status'));

  // ── W9 / C4：叙事 / 扩散 / 当前 PAD 可视化 ────────────────────────────
  Future<Map<String, dynamic>> narrativeList({int limit = 50}) async =>
      _asMap(await call('narrative.list', <String, dynamic>{'limit': limit}));

  Future<Map<String, dynamic>> narrativeReconstruct({bool incremental = true}) async =>
      _asMap(await call('narrative.reconstruct',
          <String, dynamic>{'incremental': incremental}));

  Future<List<Map<String, dynamic>>> narrativeRetryStatus() async {
    final data = await call('narrative.retry_status');
    // 兼容 WS 裸数组 / HTTP {result: ...} 包装 / {pending: [...]} 三种形态
    final pending = data is Map<String, dynamic>
        ? (data['pending'] ?? data['result'])
        : data;
    return _asList(pending).map(_asMap).toList();
  }

  Future<Map<String, dynamic>> graphDensity() async =>
      _asMap(await call('graph.density'));

  Future<Map<String, dynamic>> currentPad() async =>
      _asMap(await call('pad.current'));

  Future<Map<String, dynamic>> importMemories(String text, String suffix,
          {bool fast = true}) async =>
      _asMap(await call('import_memories', <String, dynamic>{
        'text': text, 'suffix': suffix, 'fast': fast,
      }));

  // ── 桌面能力（后端 core.* 下沉模块）───────────────
  Future<Map<String, dynamic>> systemStats() async => _asMap(await call('system.stats'));

  Future<Map<String, dynamic>> padDistribution({int limit = 1200}) async =>
      _asMap(await call('pad.distribution', <String, dynamic>{'limit': limit}));

  Future<Map<String, dynamic>> graphProjection({int limit = 1200}) async =>
      _asMap(await call('graph.projection', <String, dynamic>{'limit': limit}));

  Future<Map<String, dynamic>> autostartStatus() async => _asMap(await call('autostart.get'));

  Future<Map<String, dynamic>> setAutostart(bool enabled, String target) async =>
      _asMap(await call('autostart.set', <String, dynamic>{
        'enabled': enabled, 'target': target,
      }));

  Future<Map<String, dynamic>> secretsStatus() async => _asMap(await call('secrets.status'));

  Future<Map<String, dynamic>> readSecret(String account) async =>
      _asMap(await call('secrets.get', <String, dynamic>{'account': account}));

  Future<Map<String, dynamic>> writeSecret(String account, String value) async =>
      _asMap(await call('secrets.set', <String, dynamic>{
        'account': account, 'value': value,
      }));

  Future<Map<String, dynamic>> deleteSecret(String account) async =>
      _asMap(await call('secrets.delete', <String, dynamic>{'account': account}));

  Future<Map<String, dynamic>> dataStatus() async => _asMap(await call('data.status'));

  Future<Map<String, dynamic>> migrateData(String target, {bool move = false}) async =>
      _asMap(await call('data.migrate', <String, dynamic>{
        'target': target, 'move': move,
      }));

  Future<Map<String, dynamic>> resetDataPointer() async =>
      _asMap(await call('data.reset_pointer'));

  Future<Map<String, dynamic>> trayStatus() async => _asMap(await call('tray.status'));

  Future<Map<String, dynamic>> setTray(bool enabled) async =>
      _asMap(await call('tray.enable', <String, dynamic>{'enabled': enabled}));

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
          // 通用 HTTP RPC 兜底：WS 断开时仍能调用全部 v3 方法
          // （上下文状态 / Core Memory / 模型状态 / 网关状态 / 桌面能力 …）。
          final reply = await _post('/v1/rpc', <String, dynamic>{
            'method': method, 'args': args,
          });
          if (reply['error'] != null) {
            return <String, dynamic>{'error': reply['error'].toString()};
          }
          // 后端 /v1/rpc 有两种回包：裸结果 或 {"result": {...}} 包装。
          // 旧版只拆数组，Map 结果原样返回 → 调用方拿到的 Map 里没有 nodes，
          // 于是「后端明明有 1261 条记忆，星云却显示 0 节点」。
          final payload = reply['result'] ?? reply['items'];
          if (payload is List) return payload;
          if (payload is Map) return Map<String, dynamic>.from(payload);
          return reply;
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

  static Map<String, dynamic> _asMap(dynamic value) {
    if (value is Map<String, dynamic>) return value;
    if (value is Map) return Map<String, dynamic>.from(value);
    return <String, dynamic>{'error': '返回格式异常'};
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
  RecallHit({
    required this.id,
    required this.content,
    required this.score,
    this.memoryType = 'fact',
    this.channels = const <String>[],
    this.pad = const <double>[0, 0, 0],
  });
  final String id;
  final String content;
  final double score;
  final String memoryType;
  final List<String> channels;
  final List<double> pad;

  factory RecallHit.fromJson(Map<String, dynamic> json) => RecallHit(
        id: json['id']?.toString() ?? '',
        content: json['content']?.toString() ?? '',
        score: (json['score'] as num? ?? 0).toDouble(),
        memoryType: json['memory_type']?.toString() ?? 'fact',
        channels: _asList(json['channels'])
            .map((value) => value.toString())
            .toList(),
        pad: _asList(json['pad'])
            .map((value) => (value as num? ?? 0).toDouble())
            .toList(),
      );
}

class MemoryRow {
  MemoryRow({required this.id, required this.content, this.memoryType = 'fact', this.tier = 'buffer', this.pad = const <double>[0, 0, 0], this.updatedAtMs = 0});
  final String id;
  final String content;
  final String memoryType;
  final String tier;
  final List<double> pad;
  final int updatedAtMs;

  factory MemoryRow.fromJson(Map<String, dynamic> json) => MemoryRow(
        id: json['id']?.toString() ?? '',
        content: json['content']?.toString() ?? '',
        memoryType: json['memory_type']?.toString() ?? 'fact',
        tier: json['tier']?.toString() ?? 'buffer',
        pad: _asList(json['pad'])
            .map((value) => (value as num? ?? 0).toDouble())
            .toList(),
        updatedAtMs: json['updated_at_ms'] is num
            ? (json['updated_at_ms'] as num).toInt()
            : 0,
      );
}

/// 列表返回值兼容（库级）：WS 数组 / HTTP 兜底包装 Map 都能安全解析。
/// 后端列表类返回值有两种形态：WS 直接给数组，HTTP /v1/rpc 兜底给
/// {"items":[...], "result":[...]} 包装。任何形态都不能强转，否则一次
/// 取数类型不符就会冒泡到全局 zone 把整个应用退出（表现为「打开闪退」）。
List<dynamic> _asList(dynamic value) {
  if (value is List) return value;
  if (value is Map) {
    for (final key in <String>['result', 'items', 'memories', 'snapshots',
        'tasks', 'timeline', 'hits', 'rows', 'data']) {
      final candidate = value[key];
      if (candidate is List) return candidate;
      if (candidate is Map) {
        for (final nested in <String>['result', 'items', 'rows', 'snapshots']) {
          final inner = candidate[nested];
          if (inner is List) return inner;
        }
      }
    }
  }
  return <dynamic>[];
}
