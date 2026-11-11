// 排查：bot 存的记忆缺 PAD/类别/空间 + 数据总量核对
import 'dart:async';
import 'dart:convert';
import 'dart:io';

String cut(String s, int n) => s.length <= n ? s : s.substring(0, n);

Future<void> main() async {
  final ws = await WebSocket.connect('ws://127.0.0.1:8767/ws');
  var id = 0;
  final pending = <String, Completer<dynamic>>{};
  ws.listen((data) {
    try {
      final j = jsonDecode(data as String) as Map<String, dynamic>;
      final rid = '${j['id'] ?? ''}';
      if (pending.containsKey(rid)) pending.remove(rid)!.complete(j);
    } catch (_) {}
  }, onError: (Object e) {
    for (final c in pending.values) c.completeError(e);
    pending.clear();
  });
  Future<dynamic> rpc(String method, Map<String, dynamic> args) {
    final rid = (++id).toString();
    final c = Completer<dynamic>();
    pending[rid] = c;
    ws.add(jsonEncode(<String, dynamic>{'type': 'rpc', 'id': rid, 'method': method, 'args': args}));
    return c.future.timeout(const Duration(seconds: 20));
  }

  stdout.writeln('== 总量核对 ==');
  final topics = await rpc('topics.list', <String, dynamic>{});
  stdout.writeln('by_namespace: ${jsonEncode(topics['by_namespace'])}');

  stdout.writeln('== 最近 10 条记忆（看 source/pad/cognition/namespace）==');
  final rowsResp = await rpc('memory.list', <String, dynamic>{'limit': 10});
  stdout.writeln('memory.list ok=' + '${rowsResp['ok']}' + ' error=' + '${rowsResp['error']}');
  final rows = rowsResp['result'];
  if (rows is List) {
    for (final r in rows) {
      if (r is Map) {
        stdout.writeln('---');
        stdout.writeln('  ns=${r['namespace']} type=${r['memory_type']} pad=${r['pad']} tier=${r['tier']}');
        stdout.writeln('  content: ${cut('${r['content']}', 60)}');
      }
    }
  }
  exit(0);
}
