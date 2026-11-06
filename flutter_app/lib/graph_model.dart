import 'dart:math' as math;

import 'nebula_math.dart';

/// 布局公式移植自 astrbot_plugin_livingmemory `graph-3d-core.js`：
/// 社区球壳分布（golden angle + cbrt 距离）、社区内螺旋臂、每星系独立旋转矩阵、
/// galaxyDust 粒子云（min(24000, nodes*12)）、每社区边预算 sqrt(size)*5。
/// 所有坐标是「相对簇心的局部坐标」，渲染时由一次矩阵变换投影 —— 全量 3000+ 节点不卡。
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
  Vec3 position; // 相对所属星系中心的局部坐标
  int cluster;
  double flash = 0.0;
}

class BrainEdge {
  const BrainEdge(this.source, this.target, this.weight, this.kind);
  final String source;
  final String target;
  final double weight;
  final String kind;
}

class GalaxyCluster {
  const GalaxyCluster({
    required this.center,
    required this.radius,
    required this.spin,
    required this.tilt,
    required this.frame,
    required this.dust,
  });

  final Vec3 center;
  final double radius;
  final double spin;
  final double tilt;
  final List<double> frame; // 3x3 行主序，来自源码 frames[community*9]
  final List<Vec3> dust;    // 相对簇心的星系尘埃粒子
}

class BrainGraph {
  BrainGraph({required this.nodes, required this.edges, required this.clusters});

  final List<BrainNode> nodes;
  final List<BrainEdge> edges;
  final List<GalaxyCluster> clusters;

  Map<String, BrainNode> get byId => <String, BrainNode>{for (final n in nodes) n.id: n};

  /// 节点的静态世界坐标（不含拖拽旋转/簇自转），供光脉冲定位。
  Vec3 worldOf(BrainNode node) {
    if (node.cluster < 0 || node.cluster >= clusters.length) return node.position;
    final cluster = clusters[node.cluster];
    final f = cluster.frame;
    return cluster.center + Vec3(
      f[0] * node.position.x + f[3] * node.position.y + f[6] * node.position.z,
      f[1] * node.position.x + f[4] * node.position.y + f[7] * node.position.z,
      f[2] * node.position.x + f[5] * node.position.y + f[8] * node.position.z,
    );
  }

  factory BrainGraph.fromJson(Map<String, dynamic> json, {int? forceClusters}) {
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
        row['kind']?.toString() ?? 'semantic',
      ));
    }
    return _layout(nodes, edges, forceClusters: forceClusters);
  }

  /// ① 内容 2-gram 贪心社区检测；② 源码公式生成球壳簇心 + 螺旋臂 + 星系尘埃；
  /// ③ 每社区边预算截断。连通分量≠社区：链式边会把 3000 节点连成一个大团。
  static BrainGraph _layout(List<BrainNode> nodes, List<BrainEdge> edges, {int? forceClusters}) {
    const maxClusters = 12;
    final index = <String, int>{for (var i = 0; i < nodes.length; i++) nodes[i].id: i};

    // 连通分量不能直接当社区：链式边会把 3000 节点连成一个大团。
    // 用「内容 2-gram 余弦 + 贪心」做社区检测，代表文本截断到 160 字防 O(n²)。
    final members = <List<int>>[];
    final representatives = <String>[];
    final assigned = List<int>.filled(nodes.length, -1);
    // 簇容量上限：内容高度雷同（批量导入/同主题）时强制分散成多个星系团
    final capacity = math.max(6, (nodes.length * 1.2 / maxClusters).ceil());
    for (var i = 0; i < nodes.length; i++) {
      var best = -1;
      var bestScore = 0.0;
      for (var c = 0; c < representatives.length; c++) {
        if (members[c].length >= capacity) continue;
        final score = _similarity(nodes[i].content, representatives[c]);
        if (score > bestScore) {
          bestScore = score;
          best = c;
        }
      }
      late final int target;
      if (best >= 0 && bestScore >= 0.14) {
        target = best;
      } else if (representatives.length < maxClusters) {
        target = members.length;
        members.add(<int>[]);
        representatives.add(nodes[i].content);
      } else {
        // 所有簇都满：选当前最小的簇，保持均衡
        var smallest = 0;
        for (var c = 1; c < members.length; c++) {
          if (members[c].length < members[smallest].length) smallest = c;
        }
        target = smallest;
      }
      assigned[i] = target;
      members[target].add(i);
      final rep = representatives[target];
      if (rep.length < 160) {
        representatives[target] = rep.length + nodes[i].content.length > 160
            ? rep
            : '$rep ${nodes[i].content}';
      }
    }

    // ── 源码公式：社区球壳 + 螺旋臂 + 每星系旋转矩阵 ──────────────────
    final golden = math.pi * (3 - math.sqrt(5));
    final clusters = <GalaxyCluster>[];
    final dustTotal = math.min(24000, math.max(nodes.length * 12, 600));
    for (var c = 0; c < members.length; c++) {
      final group = members[c];
      final radius = 48 + math.sqrt(group.length.toDouble()) * 22;
      final height = 1 - 2 * (c + 0.5) / members.length;
      final circle = math.sqrt(math.max(0.0, 1 - height * height));
      final angle = c * golden;
      final distance = radius * 2 * math.pow(c, 1 / 3).toDouble();
      final center = Vec3(
        math.cos(angle) * circle * distance,
        math.sin(angle) * circle * distance,
        height * distance,
      );
      if (c == 0) {
        // 第一个星系固定在原点，避免整体漂移
        clusters.add(GalaxyCluster(center: const Vec3(0, 0, 0), radius: radius,
            spin: 0, tilt: 0, frame: <double>[1, 0, 0, 0, 1, 0, 0, 0, 1], dust: <Vec3>[]));
      } else {
        clusters.add(GalaxyCluster(center: center, radius: radius, spin: 0,
            tilt: 0, frame: <double>[1, 0, 0, 0, 1, 0, 0, 0, 1], dust: <Vec3>[]));
      }

      final yaw = c * golden + 0.35;
      final tilt = 0.45 + (c * 0.41 % 0.65);
      final cy = math.cos(yaw), sy = math.sin(yaw);
      final ct = math.cos(tilt), st = math.sin(tilt);
      final frame = <double>[cy, sy, 0, -sy * ct, cy * ct, st, sy * st, -cy * st, ct];
      final spin = 0.00012 + hashUnit('spin$c', 13) * 0.00035;

      final spiral = group.length >= 8;
      final arms = group.length >= 60 ? 3 : 2;
      for (var j = 0; j < group.length; j++) {
        final node = nodes[group[j]];
        final r = (j + 0.5) / group.length;
        final arm = j % arms;
        final a = spiral
            ? arm * math.pi * 2 / arms + 5.4 * math.sqrt(r) + math.sin(j * golden) * 0.16
            : r * math.pi * 2 + j * golden * 0.3;
        final h = spiral
            ? math.sin(j * golden * 1.3) * (0.55 * (1 - r) + 0.28)
            : 1 - 2 * ((j * math.sqrt(2)) % 1);
        final planar = spiral ? 1 / math.sqrt(1 + h * h) : math.sqrt(math.max(0.0, 1 - h * h));
        final radial = radius * (0.22 + 0.64 * math.sqrt(r));
        node.position = Vec3(
          math.cos(a) * planar * radial,
          math.sin(a) * planar * radial,
          (spiral ? h * planar : h) * radial,
        );
        node.cluster = c;
      }

      // galaxyDust：min(24000, nodes*12) 个粒子，按簇规模分摊，围绕节点形成粒子云
      final dustCount = math.max(120, (dustTotal * group.length / math.max(1, nodes.length)).round());
      final dust = <Vec3>[];
      for (var i = 0; i < dustCount; i++) {
        final node = nodes[group[i % group.length]];
        final seed = (i * 0.61803398875) % 1;
        final da = i * 2.3999632297;
        final dh = 1 - 2 * (i + 0.5) / dustCount;
        final dc = math.sqrt(math.max(0.0, 1 - dh * dh));
        final spread = radius * (0.018 + 0.07 * seed * seed);
        dust.add(node.position + Vec3(
          math.cos(da) * dc * spread,
          math.sin(da) * dc * spread,
          dh * spread,
        ));
      }
      clusters[c] = GalaxyCluster(
        center: clusters[c].center, radius: radius, spin: spin,
        tilt: tilt, frame: frame, dust: dust,
      );
    }

    // 边预算：每簇 sqrt(size)*5（12~120），每节点最多 4 条，防止 3000 节点 O(n²) 连线
    final degree = <String, int>{};
    final perCluster = <int, int>{};
    final kept = <BrainEdge>[];
    final sorted = List<BrainEdge>.from(edges)
      ..sort((a, b) => b.weight.compareTo(a.weight));
    for (final edge in sorted) {
      final a = index[edge.source], b = index[edge.target];
      if (a == null || b == null) continue;
      final c = assigned[a];
      if (c != assigned[b]) continue;
      final budget = math.min(120, math.max(12, (math.sqrt(members[c].length) * 5).floor()));
      if ((perCluster[c] ?? 0) >= budget) continue;
      if ((degree[edge.source] ?? 0) >= 4 || (degree[edge.target] ?? 0) >= 4) continue;
      perCluster[c] = (perCluster[c] ?? 0) + 1;
      degree[edge.source] = (degree[edge.source] ?? 0) + 1;
      degree[edge.target] = (degree[edge.target] ?? 0) + 1;
      kept.add(edge);
    }
    return BrainGraph(nodes: nodes, edges: kept, clusters: clusters);
  }

  /// 空间筛选（顶栏「全部 / 各分层 / 情感记忆」）。
  BrainGraph filtered(String space) {
    if (space == '全部' || space.isEmpty) return this;
    final keep = nodes.where((node) => node.tier == space || node.memoryType == space ||
        (space == '情感记忆' && node.isEmotion)).toList();
    final ids = keep.map((node) => node.id).toSet();
    return BrainGraph(
      nodes: keep,
      edges: edges.where((edge) => ids.contains(edge.source) && ids.contains(edge.target)).toList(),
      clusters: clusters,
    );
  }

  /// WS 增量：新记忆落到最匹配的簇，局部坐标围绕簇心。
  void addNode(BrainNode node) {
    if (byId.containsKey(node.id)) return;
    if (clusters.isEmpty) {
      node.position = const Vec3(0, 0, 0);
      nodes.add(node);
      return;
    }
    final c = (node.isEmotion ? clusters.length - 1 : node.memoryType.hashCode.abs() % clusters.length)
        .clamp(0, clusters.length - 1)
        .toInt();
    final cluster = clusters[c];
    final r = 20 + hashUnit(node.id, 23) * cluster.radius * 0.7;
    final a = hashUnit(node.id, 29) * math.pi * 2;
    node.position = Vec3(
      math.cos(a) * r,
      (hashUnit(node.id, 31) - 0.5) * 40,
      math.sin(a) * r * 0.55,
    );
    node.cluster = c;
    nodes.add(node);
  }

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
}

extension BrainNodeColor on BrainNode {
  int get colorValue {
    if (isEmotion || memoryType == 'emotion') return 0xFFFF4D7D;
    if (memoryType == 'profile') return 0xFFFFC857;
    if (memoryType == 'scenario') return 0xFF9B8CFF;
    return 0xFF4FD8FF;
  }
}
