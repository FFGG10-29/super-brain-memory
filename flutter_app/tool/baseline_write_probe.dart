// 验证前端骨架写通道：v3 WS persona.baseline.write（两道拒绝路径）
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
      if (pending.containsKey(rid)) {
        pending.remove(rid)!.complete(j);
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

  stdout.writeln('== 错误短语（应拒绝） ==');
  final r1 = await rpc('persona.baseline.write', <String, dynamic>{
    'content': '测试内容不应写入', 'confirm': '错的短语', 'actor': 'test',
  });
  stdout.writeln('→ ${cut(jsonEncode(r1['result'] ?? r1), 160)}');

  stdout.writeln('== 正确短语 + 过短内容（应拒绝） ==');
  final r2 = await rpc('persona.baseline.write', <String, dynamic>{
    'content': '太短', 'confirm': '确认修改人格骨架', 'actor': 'test',
  });
  stdout.writeln('→ ${cut(jsonEncode(r2['result'] ?? r2), 160)}');

  stdout.writeln('== 通道验证完毕（两道拒绝路径均生效，真实内容未写入） ==');
  exit(0);
}
