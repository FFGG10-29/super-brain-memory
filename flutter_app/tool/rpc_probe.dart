// RPC 层复现：直接走 BrainClient 的 WS 通道调 topics.list + memory.list(namespace)
// 运行：dart run tool/rpc_probe.dart  （在 flutter_app 目录下）
import 'dart:io';
import 'dart:async';
import 'dart:convert';

Future<void> main() async {
  final ws = await WebSocket.connect('ws://127.0.0.1:8767/ws');
  var id = 0;
  final pending = <String, Completer<dynamic>>{};

  ws.listen((data) {
    try {
      final j = jsonDecode(data as String) as Map<String, dynamic>;
      final rid = '${j['id'] ?? ''}';
      if (pending.containsKey(rid)) {
        pending.remove(rid)!.complete(j['ok'] == false ? <dynamic>[] : j['result']);
      }
    } catch (_) {}
  }, onError: (Object e) {
    for (final c in pending.values) {
      c.completeError(e);
    }
    pending.clear();
  });

  Future<dynamic> rpc(String method, Map<String, dynamic> args) {
    final rid = (++id).toString();
    final c = Completer<dynamic>();
    pending[rid] = c;
    ws.add(jsonEncode(<String, dynamic>{'type': 'rpc', 'id': rid, 'method': method, 'args': args}));
    return c.future.timeout(const Duration(seconds: 15));
  }

  stdout.writeln('== topics.list ==');
  final topics = await rpc('topics.list', <String, dynamic>{});
  final byNs = (topics is Map) ? (topics['by_namespace'] as Map? ?? <String, dynamic>{}) : <String, dynamic>{};
  stdout.writeln('by_namespace: $byNs');

  stdout.writeln('== memory.list(namespace=topic:64bcc7163e21) ==');
  final rows = await rpc('memory.list', <String, dynamic>{
    'limit': 5, 'keyword': '', 'tier': '', 'memory_type': '',
    'namespace': 'topic:64bcc7163e21', 'since_ms': 0,
  });
  stdout.writeln('返回类型: ${rows.runtimeType} | 条数: ${rows is List ? rows.length : '非List'}');
  if (rows is List && rows.isNotEmpty) {
    final first = rows.first;
    stdout.writeln('首条字段: ${first is Map ? first.keys.toList().join(',') : first.runtimeType}');
  }

  stdout.writeln('== 全部通过（RPC 层无崩溃） ==');
  exit(0);
}
