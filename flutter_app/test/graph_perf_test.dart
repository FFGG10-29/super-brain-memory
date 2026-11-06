import 'package:flutter_test/flutter_test.dart';

import 'package:super_brain/graph_model.dart';

/// 全量 3000+ 记忆不卡的关键：布局只算一次、粒子数有上限、边预算截断、
/// 渲染用 Canvas.drawPoints 批量绘制（约 6 次 draw call，与节点数无关）。
void main() {
  test('3000 nodes / 4000 edges lays out fast with bounded particles and edges', () {
    final nodes = <Map<String, dynamic>>[];
    final edges = <Map<String, dynamic>>[];
    for (var i = 0; i < 3000; i++) {
      nodes.add(<String, dynamic>{
        'id': 'm$i',
        'content': '主题${i % 12}：缓存服务与向量召回联动的第 $i 条记忆',
        'memory_type': i % 17 == 0 ? 'emotion' : 'fact',
        'tier': i % 3 == 0 ? 'core' : 'recall',
        'confidence': 0.6,
        'is_emotion': i % 17 == 0,
        'pad': <double>[0.1, 0.2, 0.0],
      });
      if (i > 0 && i < 2001) {
        edges.add(<String, dynamic>{'source': 'm${i - 1}', 'target': 'm$i', 'weight': 0.8, 'kind': 'semantic'});
        edges.add(<String, dynamic>{'source': 'm${i - 1}', 'target': 'm${(i * 7) % 3000}', 'weight': 0.4, 'kind': 'synapse'});
      }
    }

    final stopwatch = Stopwatch()..start();
    final graph = BrainGraph.fromJson(<String, dynamic>{'nodes': nodes, 'edges': edges});
    stopwatch.stop();

    expect(graph.nodes.length, 3000, reason: '节点全量保留');
    expect(graph.clusters.length, greaterThan(1), reason: '必须形成多个星系团');
    final dust = graph.clusters.fold<int>(0, (sum, cluster) => sum + cluster.dust.length);
    expect(dust, lessThanOrEqualTo(24000), reason: '星尘上限 min(24000, nodes*12)');
    expect(graph.edges.length, lessThan(edges.length), reason: '边预算截断，防止 O(n²) 连线');
    expect(stopwatch.elapsedMilliseconds, lessThan(3000), reason: '3000 节点布局应在 3 秒内');
    expect(graph.filtered('emotion').nodes.length, greaterThan(0), reason: '空间筛选可用');
    // ignore: avoid_print
    print('layout ${stopwatch.elapsedMilliseconds}ms · clusters ${graph.clusters.length} · '
        'dust $dust · edges ${graph.edges.length}/${edges.length}');
  });
}
