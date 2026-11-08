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

  /// 0（快遗忘）~1（刚用过）：tier 为主、confidence 微调。
  /// 中枢/黑洞径向排布、以及「这条记忆被用到就闪」都以此为依据。
  double get recency {
    final base = tier == 'core'
        ? 1.0
        : tier == 'recall'
            ? 0.72
            : tier == 'archive'
                ? 0.12
                : 0.42;
    return (base * 0.75 + confidence * 0.25).clamp(0.0, 1.0);
  }

  /// 语义分类：星团着色依据。以后端 memory_type 为准
  /// （profile / scenario / code / emotion / fact），fact 再按内容识别代码。
  /// 不用 is_emotion 兜底：后端把 PAD 愉悦度 >0.6 的都标成情绪，
  /// 会让 90% 节点变紫红，星团全糊成一个颜色。
  String get category {
    if (memoryType == 'emotion') return 'emotion';
    if (memoryType == 'profile') return 'profile';
    if (memoryType == 'scenario') return 'scenario';
    if (memoryType == 'code') return 'code';
    return _looksLikeCode(content) ? 'code' : 'fact';
  }
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
    this.tint = 0xFF7FD8FF,
  });

  final Vec3 center;
  final double radius;
  final double spin;
  final double tilt;
  final List<double> frame; // 3x3 行主序，来自源码 frames[community*9]
  final List<Vec3> dust;    // 相对簇心的星系尘埃粒子
  /// 星团主色：由簇内节点最多的语义分类决定（事实金 / 情绪紫红 / 代码青蓝…）。
  final int tint;
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
      // 簇心收紧一些：星系团彼此靠近，才是一片星云而不是散落的小球。
      final distance = radius * 1.15 * math.pow(c, 1 / 3).toDouble();
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

      // 真实星云绝不均匀：约 2/3 节点聚成 2 个致密星团核（近高斯聚集），
      // 其余沿对数螺旋「悬臂」拖出去，形成团 + 臂的层次，而不是一盘芝麻。
      for (var j = 0; j < group.length; j++) {
        final node = nodes[group[j]];
        final h1 = hashUnit(node.id, 71);
        final h2 = hashUnit(node.id, 73);
        final h3 = hashUnit(node.id, 79);
        final h4 = hashUnit(node.id, 89);
        if (h1 < 0.66) {
          // 致密星团：围绕 2 个核心之一，三段均匀和近似高斯分布
          final core = h2 < 0.5 ? 0.0 : 1.0;
          final coreA = c * golden + core * math.pi;
          final coreR = radius * (0.16 + core * 0.26);
          final gauss = (h2 + h3 + h4) / 3.0;
          final spread = radius * (0.03 + 0.20 * gauss * gauss);
          final a = coreA + h2 * math.pi * 2;
          node.position = Vec3(
            math.cos(coreA) * coreR + math.cos(a) * spread,
            math.sin(coreA) * coreR * 0.62 + math.sin(a) * spread * 0.72,
            (h3 - 0.5) * radius * 0.36 + (h4 - 0.5) * spread,
          );
        } else {
          // 对数螺旋悬臂：2 条臂，沿臂由内向外铺开
          final arm = h2 < 0.5 ? 0.0 : math.pi;
          final t = hashUnit(node.id, 97);
          final a = arm + t * 3.6 + c * 0.7;
          final r = radius * (0.30 + t * 0.78);
          final jitter = radius * 0.085 * (h3 - 0.5);
          node.position = Vec3(
            math.cos(a) * r + jitter,
            (h4 - 0.5) * radius * 0.30,
            math.sin(a) * r + jitter,
          );
        }
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
        tint: clusterTint(group.map((i) => nodes[i])),
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

    // 跨簇弱连线：相邻星系各挑一个代表节点相连，让整片星云连成网络，
    // 避免后端 2-gram 相似度稀疏时出现一簇簇互不相干的孤立小片。
    if (clusters.length > 1) {
      final reps = <int>[];
      for (var c = 0; c < clusters.length; c++) {
        final group = members[c];
        reps.add(group.isEmpty ? -1 : group[group.length ~/ 2]);
      }
      for (var c = 0; c < clusters.length; c++) {
        final next = (c + 1) % clusters.length;
        if (reps[c] < 0 || reps[next] < 0) continue;
        kept.add(BrainEdge(
          nodes[reps[c]].id, nodes[reps[next]].id, 0.08, 'cross-cluster',
        ));
      }
    }
    // 世界坐标归一化：簇心距离 ∝ c^(1/3)、簇半径 ∝ sqrt(size)，全量 1261 节点时
    // 最远簇心能到 ±800，投影后直接飞出画布 —— 屏幕只剩零星几粒，看起来像没数据。
    // 这里把「最远簇缘」压到固定 350 世界单位，保证 zoom=1 时整片星云正好入画。
    const targetExtent = 350.0;
    var farthest = 0.0;
    for (final cluster in clusters) {
      final edge = cluster.center.length + cluster.radius;
      if (edge > farthest) farthest = edge;
    }
    if (farthest > targetExtent) {
      final k = targetExtent / farthest;
      for (final node in nodes) {
        node.position = node.position * k;
      }
      for (var c = 0; c < clusters.length; c++) {
        final cluster = clusters[c];
        clusters[c] = GalaxyCluster(
          center: cluster.center * k,
          radius: cluster.radius * k,
          spin: cluster.spin,
          tilt: cluster.tilt,
          frame: cluster.frame,
          dust: cluster.dust.map((point) => point * k).toList(),
          tint: cluster.tint,
        );
      }
    }
    return BrainGraph(nodes: nodes, edges: kept, clusters: clusters);
  }

  /// 全量加载：把后端分批推送的节点先轻量追加（不做 O(n²) 聚类），
  /// 让界面边收边渲染；全部到齐后再 recluster() 一次成团。
  static BrainGraph fromRows(List<Map<String, dynamic>> rows, {int? forceClusters}) =>
      BrainGraph.fromJson(<String, dynamic>{'nodes': rows, 'edges': <dynamic>[]},
          forceClusters: forceClusters);

  void addNodes(List<BrainNode> incoming) {
    if (incoming.isEmpty) return;
    final known = byId;
    for (final node in incoming) {
      if (known.containsKey(node.id)) continue;
      known[node.id] = node;
      if (clusters.isEmpty) {
        node.position = const Vec3(0, 0, 0);
      } else {
        final c = (node.memoryType.hashCode.abs() + node.id.hashCode.abs()) % clusters.length;
        node.cluster = c;
        final cluster = clusters[c];
        final a = hashUnit(node.id, 29) * math.pi * 2;
        final r = cluster.radius * (0.22 + 0.62 * hashUnit(node.id, 23));
        node.position = Vec3(
          math.cos(a) * r,
          (hashUnit(node.id, 31) - 0.5) * r * 0.5,
          math.sin(a) * r * 0.8,
        );
      }
      nodes.add(node);
    }
  }

  /// 全部节点到位后重排成「致密星团 + 悬臂」布局。
  void recluster() {
    if (nodes.isEmpty) return;
    final rebuilt = _layout(List<BrainNode>.from(nodes), List<BrainEdge>.from(edges));
    nodes
      ..clear()
      ..addAll(rebuilt.nodes);
    edges
      ..clear()
      ..addAll(rebuilt.edges);
    clusters
      ..clear()
      ..addAll(rebuilt.clusters);
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

/// 语义分类 → 星团/节点主色（赛博银河配色：代码青蓝、情绪紫红、事实金黄）。
int categoryTint(String category) {
  switch (category) {
    case 'code':
      return 0xFF3BE8FF; // 青蓝
    case 'emotion':
      return 0xFFFF3F86; // 紫红
    case 'profile':
      return 0xFFFFD166; // 暖金
    case 'scenario':
      return 0xFF9B72FF; // 紫罗兰
    default:
      return 0xFFFFB454; // 事实金橙
  }
}

/// 星团主色 = 簇内出现最多的语义分类（同色系星团才不会糊成一片白）。
int clusterTint(Iterable<BrainNode> nodes) {
  final counts = <String, int>{};
  for (final node in nodes) {
    counts[node.category] = (counts[node.category] ?? 0) + 1;
  }
  if (counts.isEmpty) return 0xFF7FD8FF;
  final best = counts.entries.reduce((a, b) => a.value >= b.value ? a : b).key;
  return categoryTint(best);
}

final RegExp _codePattern = RegExp(
  r'```|(^|\s)(def|class|fn|func|pub fn|impl|import|from|#include|SELECT|fn main)\s'
  r'|=>|;\s*$|\{\s*$|</?[a-zA-Z][\w-]*>|\bvoid\s+\w+\(|\bint\s+\w+\s*=',
  multiLine: true,
);

/// 内容像代码（供 fact 里混入的代码类记忆单独成团）。
bool _looksLikeCode(String text) {
  if (text.length < 12) return false;
  return _codePattern.hasMatch(text);
}
