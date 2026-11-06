import 'dart:math' as math;

import 'nebula_math.dart';

/// 记忆节点：3D 坐标 + 所属星系簇。
class BrainNode {
  BrainNode({
    required this.id,
    required this.content,
    this.memoryType = 'fact',
    this.tier = 'buffer',
    this.confidence = 0.5,
    this.isEmotion = false,
    this.pad = const <double>[0, 0, 0],
    Vec3? position,
    this.cluster = 0,
  }) : position = position ?? const Vec3(0, 0, 0);

  final String id;
  final String content;
  final String memoryType;
  final String tier;
  final double confidence;
  final bool isEmotion;
  final List<double> pad;
  Vec3 position;
  int cluster;
  double flash = 0.0;
}

class BrainEdge {
  const BrainEdge(this.source, this.target, this.weight);
  final String source;
  final String target;
  final double weight;
}

class GalaxyCluster {
  const GalaxyCluster(this.center, this.spin, this.tilt);
  final Vec3 center;
  final double spin;
  final double tilt;
}

class BrainGraph {
  BrainGraph({required this.nodes, required this.edges, required this.clusters});

  final List<BrainNode> nodes;
  final List<BrainEdge> edges;
  final List<GalaxyCluster> clusters;

  Map<String, BrainNode> get byId => <String, BrainNode>{for (final n in nodes) n.id: n};

  /// 中文 2-gram TF 余弦：前端聚类用，不依赖后端相似度阈值。
  static Map<String, int> _tf(String text) {
    final clean = text.replaceAll(RegExp(r'\s+'), '');
    final freq = <String, int>{};
    for (var i = 0; i < clean.length - 1; i++) {
      final gram = clean.substring(i, i + 2);
      freq[gram] = (freq[gram] ?? 0) + 1;
    }
    return freq;
  }

  static double _similarity(String a, String b) {
    final x = _tf(a), y = _tf(b);
    if (x.isEmpty || y.isEmpty) return 0.0;
    var dot = 0;
    for (final entry in x.entries) {
      final other = y[entry.key];
      if (other != null) dot += entry.value * other;
    }
    var nx = 0.0, ny = 0.0;
    for (final value in x.values) {
      nx += value * value;
    }
    for (final value in y.values) {
      ny += value * value;
    }
    return dot / (math.sqrt(nx) * math.sqrt(ny) + 0.00001);
  }

  factory BrainGraph.fromJson(Map<String, dynamic> json, {double clusterRadius = 330, double thickness = 42}) {
    final nodes = <BrainNode>[];
    for (final raw in json['nodes'] as List<dynamic>? ?? <dynamic>[]) {
      final row = raw as Map<String, dynamic>;
      final pad = (row['pad'] as List<dynamic>? ?? <dynamic>[0, 0, 0])
          .map((v) => (v as num? ?? 0).toDouble())
          .toList();
      nodes.add(BrainNode(
        id: row['id']?.toString() ?? '',
        content: row['content']?.toString() ?? '',
        memoryType: row['memory_type']?.toString() ?? 'fact',
        tier: row['tier']?.toString() ?? 'buffer',
        confidence: (row['confidence'] as num? ?? 0.5).toDouble(),
        isEmotion: row['is_emotion'] == true,
        pad: pad,
      ));
    }
    final edges = <BrainEdge>[];
    for (final raw in json['edges'] as List<dynamic>? ?? <dynamic>[]) {
      final row = raw as Map<String, dynamic>;
      edges.add(BrainEdge(
        row['source']?.toString() ?? '',
        row['target']?.toString() ?? '',
        (row['weight'] as num? ?? 0).toDouble(),
      ));
    }
    return _layout(nodes, edges, clusterRadius, thickness);
  }

  /// 语义聚类：先按突触边求连通分量，孤立节点按记忆类型归簇；
  /// 每个簇是独立的「星系盘」，簇心在球壳上分散 —— 这就是"一坨一坨"的来源。
  static BrainGraph _layout(List<BrainNode> nodes, List<BrainEdge> edges, double clusterRadius, double thickness) {
    final index = <String, int>{for (var i = 0; i < nodes.length; i++) nodes[i].id: i};
    final parent = List<int>.generate(nodes.length, (i) => i);

    int find(int i) {
      while (parent[i] != i) {
        parent[i] = parent[parent[i]];
        i = parent[i];
      }
      return i;
    }

    void union(int a, int b) {
      final ra = find(a), rb = find(b);
      if (ra != rb) parent[rb] = ra;
    }

    for (final edge in edges) {
      final a = index[edge.source], b = index[edge.target];
      if (a != null && b != null) union(a, b);
    }

    // ① 先用真实突触边求连通分量作为种子簇；
    // ② 其余节点用内容 2-gram 相似度贪心归簇（后端边稀疏时也保证形成多个星系团）；
    // ③ 最多 maxClusters 个簇，超出并入最相似的簇。
    const maxClusters = 10;
    final seedGroups = <int, List<int>>{};
    for (var i = 0; i < nodes.length; i++) {
      seedGroups.putIfAbsent(find(i), () => <int>[]).add(i);
    }
    final representatives = <String>[];
    final rootClusterIds = <int, int>{};
    var nextCluster = 0;

    final sortedRoots = seedGroups.keys.toList()
      ..sort((a, b) => seedGroups[b]!.length.compareTo(seedGroups[a]!.length));
    for (final root in sortedRoots) {
      final members = seedGroups[root]!;
      final hasEdge = members.length > 1;
      if (!hasEdge) continue; // 单节点组交给贪心阶段
      if (nextCluster >= maxClusters) break;
      final id = nextCluster++;
      rootClusterIds[root] = id;
      representatives.add(members.take(3).map((i) => nodes[i].content).join(' '));
    }

    for (var i = 0; i < nodes.length; i++) {
      final root = find(i);
      final preset = rootClusterIds[root];
      if (preset != null) {
        nodes[i].cluster = preset;
        continue;
      }
      var best = -1;
      var bestScore = 0.0;
      for (var c = 0; c < representatives.length; c++) {
        final score = _similarity(nodes[i].content, representatives[c]);
        if (score > bestScore) {
          bestScore = score;
          best = c;
        }
      }
      if (best >= 0 && bestScore >= 0.12) {
        nodes[i].cluster = best;
      } else if (representatives.length < maxClusters) {
        nodes[i].cluster = representatives.length;
        representatives.add(nodes[i].content);
      } else if (best >= 0) {
        nodes[i].cluster = best;
      } else {
        nodes[i].cluster = 0;
      }
      // 代表文本随簇增长缓慢扩充，保持簇心语义稳定
      if (representatives.isNotEmpty && bestScore > 0) {
        representatives[nodes[i].cluster] =
            '${representatives[nodes[i].cluster]} ${nodes[i].content}';
      }
    }

    final groups = <int, List<BrainNode>>{};
    for (final node in nodes) {
      groups.putIfAbsent(node.cluster, () => <BrainNode>[]).add(node);
    }
    final clusterIds = groups.keys.toList()..sort();
    final clusters = <GalaxyCluster>[];
    final golden = math.pi * (1 + math.sqrt(5));

    for (var c = 0; c < clusterIds.length; c++) {
      final id = clusterIds[c];
      final members = groups[id]!;
      final phi = math.acos(1 - 2 * (c + 0.5) / clusterIds.length);
      final theta = golden * c;
      final radius = clusterRadius * (0.62 + 0.38 * hashUnit('cluster$c', 7));
      final center = Vec3(
        math.sin(phi) * math.cos(theta) * radius,
        math.sin(phi) * math.sin(theta) * radius * 0.62,
        math.cos(phi) * radius * 0.8,
      );
      final tilt = (hashUnit('tilt$c', 11) - 0.5) * 1.1;
      clusters.add(GalaxyCluster(center, 0.00015 + hashUnit('spin$c', 13) * 0.0005, tilt));

      for (var j = 0; j < members.length; j++) {
        final node = members[j];
        final t = (j + 0.5) / members.length;
        final diskR = 18 + 105 * math.sqrt(t);
        final arm = (j % 2 == 0 ? 0.0 : math.pi);
        final angle = j * 2.39996 + c * 0.7 + diskR * 0.014 + arm;
        final flatX = math.cos(angle) * diskR;
        final flatZ = math.sin(angle) * diskR;
        final vertical = (hashUnit(node.id, 17) - 0.5) * thickness * (1 - t) +
            math.sin(tilt) * flatZ * 0.45;
        node.position = Vec3(
          center.x + flatX + (hashUnit(node.id, 3) - 0.5) * 14,
          center.y + vertical + (hashUnit(node.id, 5) - 0.5) * 12,
          center.z + flatZ * 0.55 + (hashUnit(node.id, 9) - 0.5) * 14,
        );
        node.cluster = c;
      }
    }
    return BrainGraph(nodes: nodes, edges: edges, clusters: clusters);
  }

  /// WS 增量：新记忆落进最匹配的簇（按类型），位置围绕簇心。
  void addNode(BrainNode node) {
    if (byId.containsKey(node.id)) return;
    if (clusters.isEmpty) {
      node.position = const Vec3(0, 0, 0);
      nodes.add(node);
      return;
    }
    final count = clusters.length;
    final c = (node.isEmotion ? count - 1 : node.memoryType.hashCode.abs() % count)
        .clamp(0, count - 1)
        .toInt();
    final center = clusters[c].center;
    final r = 30 + hashUnit(node.id, 23) * 110;
    final a = hashUnit(node.id, 29) * math.pi * 2;
    node.position = Vec3(
      center.x + math.cos(a) * r,
      center.y + (hashUnit(node.id, 31) - 0.5) * 40,
      center.z + math.sin(a) * r * 0.55,
    );
    node.cluster = c;
    nodes.add(node);
  }
}

/// 纯数据层不依赖 Flutter：颜色用 ARGB 整数，painter 层转成 Color。
extension BrainNodeColor on BrainNode {
  int get colorValue {
    if (isEmotion || memoryType == 'emotion') return 0xFFFF4D7D;
    if (memoryType == 'profile') return 0xFFFFC857;
    if (memoryType == 'scenario') return 0xFF9B8CFF;
    return 0xFF4FD8FF;
  }
}
