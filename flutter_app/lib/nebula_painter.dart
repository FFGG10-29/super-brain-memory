import 'dart:math' as math;
import 'dart:ui' as ui;

import 'package:flutter/material.dart';

import 'graph_model.dart';
import 'nebula_math.dart';

class NebulaStyle {
  const NebulaStyle({
    this.focal = 900,
    this.starSize = 3.2,
    this.bloom = true,
    this.showLabels = true,
    this.autoRotate = true,
    this.spinSpeed = 0.00035,
    this.galaxyThickness = 42,
    this.showEdges = false,
    this.showBlackHole = true,
    this.showCore = true,
    this.twinkle = true,
    this.recencyWarp = true,
    this.showGravity = true,
    this.parallax = true,
    this.fullMode = false,
  });

  final double focal;
  final double starSize;
  final bool bloom;
  final bool showLabels;
  final bool autoRotate;
  final double spinSpeed;
  final double galaxyThickness;
  final bool showEdges;
  final bool showBlackHole;
  final bool showCore;
  final bool twinkle;
  final bool recencyWarp;
  final bool showGravity;
  final bool parallax;
  final bool fullMode;
}

class Pulse3D {
  Pulse3D({required this.from, required this.to, this.similarity = 0.8});
  final Vec3 from;
  Vec3 to;
  final double similarity;
  double progress = 0.0;
  bool get done => progress >= 1.0;
  void step() => progress += 0.012 + similarity * 0.018;
}

/// GPU 友好渲染：所有粒子/节点/连线都用 drawPoints 批量绘制。
/// 视觉升维：景深分组（近大亮远小暗）、三层视差星尘、脉冲发光拖尾、
/// 空间扭曲黑洞、白色恒星中枢 + 引力线；并按 zoom / 节点数做 LOD 保命。
class NebulaPainter extends CustomPainter {
  NebulaPainter({
    required this.graph,
    required this.yaw,
    required this.pitch,
    required this.zoom,
    required this.spinTime,
    required this.pulses,
    required this.style,
    this.highlighted = const <String>{},
    this.stardust = const <List<double>>[],
    this.burst = 0.0,
    this.coreBurst = 0.0,
  });

  final BrainGraph graph;
  final double yaw;
  final double pitch;
  final double zoom;
  final double spinTime;
  final List<Pulse3D> pulses;
  final NebulaStyle style;
  final Set<String> highlighted;
  final List<List<double>> stardust;
  final double burst;
  final double coreBurst;

  @override
  void paint(Canvas canvas, Size size) {
    final rect = Offset.zero & size;
    canvas.drawRect(
      rect,
      Paint()
        ..shader = const RadialGradient(
          center: Alignment(0, -0.12),
          radius: 1.3,
          colors: <Color>[Color(0xFF0A1528), Color(0xFF05080E), Color(0xFF010205)],
          stops: <double>[0.0, 0.55, 1.0],
        ).createShader(rect),
    );

    // LOD：镜头拉远 / 全量模式时逐级削减细节，保证 3000+ 节点不掉帧。
    var lod = zoom < 0.72 ? 2 : (zoom < 1.18 ? 1 : 0);
    if (style.fullMode && lod == 0 && zoom < 1.45) lod = 1;

    _drawNebulaClouds(canvas, size, spinTime);
    final center = Offset(size.width / 2, size.height / 2);
    if (style.showCore) _drawCore(canvas, center, spinTime);
    if (style.showBlackHole) {
      _drawBlackHole(canvas, Offset(size.width * 0.88, size.height * 0.66), spinTime);
    }

    final cy = math.cos(yaw), sy = math.sin(yaw);
    final cp = math.cos(pitch), sp = math.sin(pitch);
    final cx = size.width / 2, cyy = size.height / 2;

    Projected proj(Vec3 world) => project(
          world, focal: style.focal, cx: cx, cy: cyy, zoom: zoom,
        );

    final clusterData = <_ClusterTransform>[];
    for (final cluster in graph.clusters) {
      final a = spinTime * cluster.spin;
      clusterData.add(_ClusterTransform(cluster: cluster, ca: math.cos(a), sa: math.sin(a)));
    }

    Vec3 transform(Vec3 local, _ClusterTransform t) {
      final f = t.cluster.frame;
      final x1 = local.x * t.ca - local.z * t.sa;
      final z1 = local.x * t.sa + local.z * t.ca;
      final wx = t.cluster.center.x + f[0] * x1 + f[3] * local.y + f[6] * z1;
      final wy = t.cluster.center.y + f[1] * x1 + f[4] * local.y + f[7] * z1;
      final wz = t.cluster.center.z + f[2] * x1 + f[5] * local.y + f[8] * z1;
      final rx = wx * cy - wz * sy;
      final rz = wx * sy + wz * cy;
      return Vec3(rx, wy * cp - rz * sp, wy * sp + rz * cp);
    }

    // ── ① 三层视差星尘：拖拽 / 缩放时以不同速度移动 ───────────────────
    if (style.parallax && stardust.isNotEmpty) {
      final layers = <List<Offset>>[<Offset>[], <Offset>[], <Offset>[]];
      final factors = <double>[0.12, 0.38, 0.85];
      for (final star in stardust) {
        final layer = star[3].toInt().clamp(0, 2);
        final f = factors[layer];
        // 缩放时近层位移更大 → 强 3D 视差
        final depthZoom = 1.0 + (zoom - 1.0) * f * 0.55;
        final dx = math.sin(yaw * f) * 90 * f;
        final dy = math.cos(pitch * f) * 62 * f;
        final x = ((star[0] - cx) * depthZoom + cx + dx) % size.width;
        final y = ((star[1] - cyy) * depthZoom + cyy + dy) % size.height;
        layers[layer].add(Offset(x, y));
      }
      final colors = <Color>[
        const Color(0x1E6E90C8),
        const Color(0x3C8FB4E6),
        const Color(0x77BFE0FF),
      ];
      final widths = <double>[0.9, 1.25, 1.8];
      for (var i = 0; i < 3; i++) {
        if (layers[i].isEmpty) continue;
        canvas.drawPoints(
          ui.PointMode.points,
          layers[i],
          Paint()
            ..color = colors[i]
            ..strokeWidth = widths[i]
            ..strokeCap = StrokeCap.round
            ..blendMode = BlendMode.plus,
        );
      }
    }

    // ── ② 星团云气：每个星系一团同色柔和辉光，撑起「星云」的体积感 ────
    // 关键：结构必须由「面」来表现，不能指望点。1261 个点堆在一起只会是噪声；
    // 一团 tint 色的径向渐隐垫底，星点再叠上去才有星云的样子。
    if (lod < 2) {
      for (final t in clusterData) {
        final c = proj(transform(const Vec3(0, 0, 0), t));
        if (c.x < -size.width * 0.3 || c.x > size.width * 1.3 ||
            c.y < -size.height * 0.3 || c.y > size.height * 1.3) {
          continue;
        }
        final r = (t.cluster.radius * c.scale * zoom * 1.85).clamp(22.0, size.width * 0.46);
        final tint = Color(t.cluster.tint);
        _glow(canvas, Offset(c.x, c.y), r, <Color>[
          tint.withValues(alpha: 0.20),
          tint.withValues(alpha: 0.09),
          const Color(0x00000000),
        ], const <double>[0.0, 0.42, 1.0]);
      }
    }

    // ── ③ 星系尘埃云（按星团主色染色；LOD2 直接跳过，最省帧的一步）──────
    if (lod < 2) {
      final dustByTint = <int, List<Offset>>{};
      final hazePoints = <Offset>[];
      final stride = lod == 1 ? 2 : 1;
      for (final t in clusterData) {
        final dust = t.cluster.dust;
        for (var i = 0; i < dust.length; i += stride) {
          final p = proj(transform(dust[i], t));
          if (p.x < -80 || p.x > size.width + 80 || p.y < -80 || p.y > size.height + 80) continue;
          final point = Offset(p.x, p.y);
          (dustByTint[t.cluster.tint] ??= <Offset>[]).add(point);
          if (i % 19 == 0 && style.bloom && lod == 0) hazePoints.add(point);
        }
      }
      dustByTint.forEach((tint, points) {
        if (points.isEmpty) return;
        canvas.drawPoints(
          ui.PointMode.points,
          points,
          Paint()
            ..color = Color(tint).withValues(alpha: 0.16)
            ..strokeWidth = 0.9
            ..strokeCap = StrokeCap.round
            ..blendMode = BlendMode.plus,
        );
      });
      if (hazePoints.isNotEmpty) {
        canvas.drawPoints(
          ui.PointMode.points,
          hazePoints,
          Paint()
            ..color = const Color(0x149ED0FF)
            ..strokeWidth = 8.0
            ..strokeCap = StrokeCap.round
            ..blendMode = BlendMode.plus,
        );
      }
    }

    // ── ③ 节点投影 + 景深分组 + 轨道旋转 ───────────────────────────────
    final world = <String, Vec3>{};
    final projected = <String, Projected>{};
    // band 0=远 1=中 2=近；每个 band 内再按语义分类分桶染色
    // （事实金 / 情绪紫红 / 代码青蓝 / 画像暖金 / 场景紫罗兰）。
    final bands = <Map<int, List<Offset>>>[
      <int, List<Offset>>{},
      <int, List<Offset>>{},
      <int, List<Offset>>{},
    ];
    final twinklePoints = <Offset>[];
    final flashPoints = <Offset>[];
    final gravityTargets = <Offset>[];

    for (final node in graph.nodes) {
      final t = clusterData.isEmpty
          ? null
          : clusterData[node.cluster.clamp(0, clusterData.length - 1).toInt()];
      var w = t == null ? rotate(node.position, yaw, pitch) : transform(node.position, t);

      // 高频使用的记忆围绕中枢做缓慢轨道旋转（越近转得越快）。
      if (style.showGravity && node.recency > 0.62) {
        final orbit = spinTime * (0.035 + node.recency * 0.055);
        final co = math.cos(orbit), so = math.sin(orbit);
        w = Vec3(w.x * co - w.z * so, w.y, w.x * so + w.z * co);
      }
      if (style.recencyWarp) {
        final scale = 0.42 + (1.0 - node.recency) * 1.10;
        w = Vec3(w.x * scale, w.y * scale, w.z * scale);
      }
      final p = proj(w);
      world[node.id] = w;
      projected[node.id] = p;
      if (p.x.isNaN || p.y.isNaN) continue;
      if (p.x < -40 || p.x > size.width + 40 || p.y < -40 || p.y > size.height + 40) continue;
      final point = Offset(p.x, p.y);

      // 景深：w = focal/(focal+z)，越大越近。近处大亮带光晕，远处小而淡。
      final closeness = (p.scale / zoom).clamp(0.0, 3.0);
      final band = closeness >= 1.08 ? 2 : (closeness >= 0.78 ? 1 : 0);
      final color = categoryTint(node.category);
      (bands[band][color] ??= <Offset>[]).add(point);

      if (lod == 0 && style.twinkle) {
        final phase = (node.id.hashCode & 0x7fffffff) / 0x7fffffff * math.pi * 2;
        if (math.sin(spinTime * 2.1 + phase) > 0.90) twinklePoints.add(point);
      }
      if (node.flash > 0.02 && lod < 2) flashPoints.add(point);
      // 中枢引力线只连最近、最高频的一批节点，避免全屏蛛网。
      if (style.showGravity && lod == 0 && node.recency > 0.72 &&
          gravityTargets.length < 46 && closeness > 0.9) {
        gravityTargets.add(point);
      }
    }

    // 中枢引力线：极细、极淡，只在高倍近景出现。
    if (style.showCore && style.showGravity && gravityTargets.isNotEmpty) {
      final lines = <Offset>[];
      for (final target in gravityTargets) {
        lines.add(center);
        lines.add(target);
      }
      canvas.drawPoints(
        ui.PointMode.lines,
        lines,
        Paint()
          ..color = const Color(0x129FE8FF)
          ..strokeWidth = 0.7
          ..blendMode = BlendMode.plus,
      );
    }

    // ── ④ 突触边：正弦呼吸，像电流一样明暗流动 ─────────────────────────
    if (style.showEdges && lod < 2) {
      const phases = 6;
      final buckets = List<List<Offset>>.generate(phases, (_) => <Offset>[]);
      var edgeIndex = 0;
      for (final edge in graph.edges) {
        final a = projected[edge.source], b = projected[edge.target];
        if (a == null || b == null) continue;
        final bucket = (edgeIndex++ + (edge.source.hashCode & 7)) % phases;
        buckets[bucket]
          ..add(Offset(a.x, a.y))
          ..add(Offset(b.x, b.y));
      }
      for (var i = 0; i < phases; i++) {
        if (buckets[i].isEmpty) continue;
        // 每桶相位错开：整片连线呈波状明暗，而不是一起闪。
        final wave = 0.5 + 0.5 * math.sin(spinTime * 1.7 + i * math.pi / 3);
        canvas.drawPoints(
          ui.PointMode.lines,
          buckets[i],
          Paint()
            ..color = Color.fromRGBO(178, 150, 240, 0.14 + 0.26 * wave)
            ..strokeWidth = 0.75 + 0.35 * wave
            ..blendMode = BlendMode.plus,
        );
      }
    }

    // ── ⑤ 检索星爆 ────────────────────────────────────────────────────
    if (burst > 0 && highlighted.isNotEmpty && lod < 2) {
      final burstLines = <Offset>[];
      final focus = proj(const Vec3(0, 0, 0));
      var seed = 0;
      for (final id in highlighted.take(20)) {
        final target = projected[id];
        if (target == null) continue;
        for (var k = 0; k < 6; k++) {
          seed++;
          final jitter = (hashUnit('$id-$k', seed) - 0.5) * burst * 42;
          burstLines.add(Offset(focus.x, focus.y));
          burstLines.add(Offset(target.x + jitter, target.y + jitter * 0.6));
        }
      }
      if (burstLines.isNotEmpty) {
        canvas.drawPoints(
          ui.PointMode.lines,
          burstLines,
          Paint()
            ..color = Color.fromRGBO(244, 209, 155, 0.20 * burst)
            ..strokeWidth = 0.7
            ..blendMode = BlendMode.plus,
        );
      }
    }

    // ── ⑥ 景深节点：远 → 中 → 近，按语义分类分桶染色 ────────────────────
    void paintBand(int index, double alpha, double width, {double lighten = 0.0}) {
      bands[index].forEach((colorValue, pts) {
        if (pts.isEmpty) return;
        var color = Color(colorValue);
        if (lighten > 0) color = Color.lerp(color, Colors.white, lighten)!;
        canvas.drawPoints(
          ui.PointMode.points,
          pts,
          Paint()
            ..color = color.withValues(alpha: alpha)
            ..strokeWidth = width
            ..strokeCap = StrokeCap.round
            ..blendMode = BlendMode.plus,
        );
      });
    }

    if (lod == 2) {
      // 宏观视野：所有节点简化成小点，无光晕、无标签、无连线。
      paintBand(0, 0.42, 1.1);
      paintBand(1, 0.62, math.max(1.1, 1.4 * zoom));
      paintBand(2, 0.86, math.max(1.2, 1.7 * zoom));
    } else {
      // 远处：极小极暗，只当背景尘埃，星团轮廓由上面的云气负责。
      paintBand(0, 0.26, 0.9);
      paintBand(1, 0.62, math.max(1.1, style.starSize * zoom * 0.34));
      // 只有最靠近镜头的一小撮才给光晕：1200+ 点全给 bloom 会糊成肥皂泡。
      if (style.bloom && lod == 0) {
        bands[2].forEach((colorValue, pts) {
          if (pts.isEmpty) return;
          canvas.drawPoints(
            ui.PointMode.points, pts,
            Paint()
              ..color = Color(colorValue).withValues(alpha: 0.06)
              ..strokeWidth = 4.6 * zoom
              ..strokeCap = StrokeCap.round
              ..blendMode = BlendMode.plus,
          );
        });
      }
      // 近处核心：直径 1.4~2.4px，保持「针尖星点」而不是光球。
      paintBand(2, 0.92, math.max(1.4, style.starSize * zoom * 0.58), lighten: 0.35);
    }

    if (lod == 0 && twinklePoints.isNotEmpty) {
      canvas.drawPoints(
        ui.PointMode.points, twinklePoints,
        Paint()
          ..color = const Color(0x558FE8FF)
          ..strokeWidth = 3.6 * zoom
          ..strokeCap = StrokeCap.round
          ..blendMode = BlendMode.plus,
      );
    }
    if (flashPoints.isNotEmpty) {
      canvas.drawPoints(
        ui.PointMode.points, flashPoints,
        Paint()
          ..color = const Color(0xAAFFD9A0)
          ..strokeWidth = 10.0 * zoom
          ..strokeCap = StrokeCap.round
          ..blendMode = BlendMode.plus,
      );
      canvas.drawPoints(
        ui.PointMode.points, flashPoints,
        Paint()
          ..color = Colors.white
          ..strokeWidth = 2.4 * zoom
          ..strokeCap = StrokeCap.round
          ..blendMode = BlendMode.plus,
      );
    }

    // ── ⑦ 标签：只在近景 + 未开启 LOD 削减时渲染 ────────────────────────
    if (style.showLabels && lod == 0) {
      var labels = 0;
      for (final id in highlighted) {
        final p = projected[id];
        final node = graph.byId[id];
        if (p == null || node == null || labels >= 8) continue;
        labels++;
        canvas.drawCircle(
          Offset(p.x, p.y), 7.0 * p.scale,
          Paint()
            ..color = const Color(0x88B4EBFF)
            ..style = PaintingStyle.stroke
            ..strokeWidth = 1.0,
        );
        final painter = TextPainter(
          text: TextSpan(text: _short(node.content),
              style: const TextStyle(color: Color(0xFFD7E6F8), fontSize: 10)),
          textDirection: ui.TextDirection.ltr,
        )..layout();
        painter.paint(canvas, Offset(p.x + 10, p.y - 12));
      }
    }

    // ── ⑧ 光脉冲 + 渐隐发光拖尾（三段批量绘制）──────────────────────────
    if (pulses.isNotEmpty) {
      final heads = <Offset>[];
      final mids = <Offset>[];
      final tails = <Offset>[];
      for (final pulse in pulses) {
        heads.add(_pulsePoint(pulse, pulse.progress, proj));
        for (var k = 1; k <= 9; k++) {
          final t = pulse.progress - k * 0.032;
          if (t < 0) break;
          final p = _pulsePoint(pulse, t, proj);
          if (k <= 3) {
            mids.add(p);
          } else {
            tails.add(p);
          }
        }
      }
      if (tails.isNotEmpty) {
        canvas.drawPoints(ui.PointMode.points, tails,
            Paint()
              ..color = const Color(0x226FD8FF)
              ..strokeWidth = 2.2
              ..strokeCap = StrokeCap.round
              ..blendMode = BlendMode.plus);
      }
      if (mids.isNotEmpty) {
        canvas.drawPoints(ui.PointMode.points, mids,
            Paint()
              ..color = const Color(0x66B5E8FF)
              ..strokeWidth = 4.0
              ..strokeCap = StrokeCap.round
              ..blendMode = BlendMode.plus);
      }
      if (heads.isNotEmpty) {
        canvas.drawPoints(ui.PointMode.points, heads,
            Paint()
              ..color = const Color(0x55FFFFFF)
              ..strokeWidth = 9.0
              ..strokeCap = StrokeCap.round
              ..blendMode = BlendMode.plus);
        canvas.drawPoints(ui.PointMode.points, heads,
            Paint()
              ..color = Colors.white
              ..strokeWidth = 2.8
              ..strokeCap = StrokeCap.round
              ..blendMode = BlendMode.plus);
      }
    }

    // 中枢爆发放在最后叠加：有光脉冲激活时一次耀眼的白色 Bloom。
    if (style.showCore && coreBurst > 0.01) {
      _glow(canvas, center, 300 * (0.4 + coreBurst),
          <Color>[
            Color.fromRGBO(255, 255, 255, 0.30 * coreBurst),
            Color.fromRGBO(150, 220, 255, 0.12 * coreBurst),
            const Color(0x00000000),
          ],
          const <double>[0.0, 0.35, 1.0]);
    }
  }

  Offset _pulsePoint(Pulse3D pulse, double t, Projected Function(Vec3) proj) {
    final p = proj(rotate(pulse.from.lerp(pulse.to, t), yaw, pitch));
    return Offset(p.x, p.y);
  }

  void _glow(Canvas canvas, Offset center, double radius,
      List<Color> colors, List<double> stops,
      {bool additive = true}) {
    if (radius <= 0) return;
    final rect = Rect.fromCircle(center: center, radius: radius);
    final paint = Paint()
      ..shader = RadialGradient(colors: colors, stops: stops).createShader(rect);
    if (additive) paint.blendMode = BlendMode.plus;
    canvas.drawCircle(center, radius, paint);
  }

  void _drawNebulaClouds(Canvas canvas, Size size, double t) {
    final w = size.width;
    final h = size.height;
    final drift = math.sin(t * 0.035) * 18;
    _glow(canvas, Offset(w * 0.28 + drift, h * 0.32), w * 0.42,
        <Color>[const Color(0x0A2F6FD8), const Color(0x00000000)],
        const <double>[0.0, 1.0]);
    _glow(canvas, Offset(w * 0.78 - drift, h * 0.70), w * 0.36,
        <Color>[const Color(0x0C6A4FC0), const Color(0x00000000)],
        const <double>[0.0, 1.0]);
    _glow(canvas, Offset(w * 0.55, h * 0.88 + drift * 0.5), w * 0.30,
        <Color>[const Color(0x081C7A8C), const Color(0x00000000)],
        const <double>[0.0, 1.0]);
  }

  /// 中枢 = 一颗白色恒星：没有雷达式同心圆环，只有径向气辉、
  /// 衍射星芒与轨道星尘。亮度刻意压暗 30%，让成百上千的彩色星团当主角。
  void _drawCore(Canvas canvas, Offset center, double t) {
    final breath = 1.0 + 0.045 * math.sin(t * 1.1);
    _glow(canvas, center, 210 * breath,
        <Color>[
          const Color(0x0A2E78C8),
          const Color(0x051B4A9C),
          const Color(0x00000000),
        ],
        const <double>[0.0, 0.40, 1.0]);
    _glow(canvas, center, 78 * breath,
        <Color>[
          const Color(0x289FE0FF),
          const Color(0x0E3A86D0),
          const Color(0x00000000),
        ],
        const <double>[0.0, 0.30, 1.0]);
    // 白色核心：中心纯白但整体降了 30% 不透明度，避免抢走星团视线。
    _glow(canvas, center, 27 * breath,
        <Color>[
          const Color(0xC8FFFFFF),
          const Color(0x7AD8F4FF),
          const Color(0x00000000),
        ],
        const <double>[0.0, 0.16, 1.0]);

    final spike = Paint()
      ..color = const Color(0x12DCF6FF)
      ..strokeWidth = 1.0
      ..blendMode = BlendMode.plus;
    for (var i = 0; i < 10; i++) {
      final a = i * math.pi / 5 + t * 0.018;
      final len = (i % 2 == 0 ? 84.0 : 30.0) * breath;
      canvas.drawLine(center,
          Offset(center.dx + math.cos(a) * len, center.dy + math.sin(a) * len * 0.72),
          spike);
    }

    // 轨道星尘：三条不同半径/速度的环带，扁椭圆形成盘面感。
    final rings = <List<Offset>>[<Offset>[], <Offset>[], <Offset>[]];
    for (var i = 0; i < 150; i++) {
      final layer = i % 3;
      final n = (i * 0.618033988749895) % 1.0;
      final m = (i * 0.7548776662466927) % 1.0;
      final a = n * math.pi * 2 + t * (0.03 + layer * 0.018);
      final r = (26 + layer * 38 + m * 22) * breath;
      rings[layer].add(Offset(
        center.dx + math.cos(a) * r,
        center.dy + math.sin(a) * r * (0.42 + layer * 0.10),
      ));
    }
    for (var i = 0; i < 3; i++) {
      canvas.drawPoints(
        ui.PointMode.points, rings[i],
        Paint()
          ..color = Color.fromRGBO(140, 210, 255, 0.08 + i * 0.025)
          ..strokeWidth = 1.1 + i * 0.25
          ..strokeCap = StrokeCap.round
          ..blendMode = BlendMode.plus,
      );
    }
    canvas.drawPoints(
      ui.PointMode.points, <Offset>[center],
      Paint()
        ..color = const Color(0xD8FFFFFF)
        ..strokeWidth = 5.5 * breath
        ..strokeCap = StrokeCap.round
        ..blendMode = BlendMode.plus,
    );
  }

  /// 黑洞 = 空间扭曲点：中心纯黑、边缘向内扭曲的半透明径向渐变 +
  /// 极细紫红吸积盘（MaskFilter.blur）。整体缩到原来的 ~62%，
  /// 只作为画面角落的景深道具，不能抢占屏幕。
  void _drawBlackHole(Canvas canvas, Offset center, double t) {
    // 引力透镜周围的冷色云气
    _glow(canvas, center, 122,
        <Color>[
          const Color(0x00000000),
          const Color(0x0C3A2A86),
          const Color(0x126A3AB8),
          const Color(0x00000000),
        ],
        const <double>[0.0, 0.34, 0.66, 1.0]);

    // 吸积盘：扁椭圆 + 高斯模糊，纯紫红，无任何描边。
    canvas.save();
    canvas.translate(center.dx, center.dy);
    canvas.rotate(t * 0.045);
    canvas.scale(1.0, 0.29);
    final discRect = Rect.fromCircle(center: Offset.zero, radius: 96);
    final disc = Paint()
      ..shader = const RadialGradient(
        colors: <Color>[
          Color(0x00000000),
          Color(0x00000000),
          Color(0x36D858BC),
          Color(0x248A48D8),
          Color(0x00000000),
        ],
        stops: <double>[0.0, 0.50, 0.68, 0.83, 1.0],
      ).createShader(discRect)
      ..maskFilter = ui.MaskFilter.blur(ui.BlurStyle.normal, 9)
      ..blendMode = BlendMode.plus;
    canvas.drawCircle(Offset.zero, 96, disc);
    canvas.restore();

    // 向内扭曲的螺旋丝：对数螺旋点串 + 模糊，模拟引力透镜拉伸。
    final swirl = <Offset>[];
    for (var i = 0; i < 240; i++) {
      final f = i / 240.0;
      final a = f * 7.6 + t * 0.22;
      final r = 14 + f * 68;
      swirl.add(Offset(
        center.dx + math.cos(a) * r,
        center.dy + math.sin(a) * r * 0.30,
      ));
    }
    canvas.drawPoints(
      ui.PointMode.points, swirl,
      Paint()
        ..color = const Color(0x22C98CD8)
        ..strokeWidth = 1.6
        ..strokeCap = StrokeCap.round
        ..maskFilter = ui.MaskFilter.blur(ui.BlurStyle.normal, 4)
        ..blendMode = BlendMode.plus,
    );

    // 事件视界：中心纯黑，向外渐隐（普通混合才能遮住背后星尘，无硬边）。
    _glow(canvas, center, 38,
        <Color>[
          const Color(0xFF000104),
          const Color(0xFA000104),
          const Color(0x8C010206),
          const Color(0x00010206),
        ],
        const <double>[0.0, 0.48, 0.76, 1.0],
        additive: false);

    // 极细紫红光子环：模糊渐变带，不是一根实线。
    final photonRect = Rect.fromCircle(center: center, radius: 44);
    canvas.drawCircle(
      center, 44,
      Paint()
        ..shader = const RadialGradient(
          colors: <Color>[
            Color(0x00000000),
            Color(0x00000000),
            Color(0x2EE07CC8),
            Color(0x00000000),
          ],
          stops: <double>[0.0, 0.78, 0.88, 1.0],
        ).createShader(photonRect)
        ..maskFilter = ui.MaskFilter.blur(ui.BlurStyle.normal, 6)
        ..blendMode = BlendMode.plus,
    );
  }

  static String _short(String text) {
    final clean = text.replaceAll(RegExp(r'\s+'), ' ');
    return clean.length > 14 ? '${clean.substring(0, 14)}…' : clean;
  }

  @override
  bool shouldRepaint(covariant NebulaPainter old) => true;
}

class _ClusterTransform {
  const _ClusterTransform({required this.cluster, required this.ca, required this.sa});
  final GalaxyCluster cluster;
  final double ca;
  final double sa;
}

/// 右下角小地图：俯视 XZ 平面，画所有星系团 + 当前镜头位置/朝向。
class MiniMapPainter extends CustomPainter {
  const MiniMapPainter({
    required this.clusters,
    required this.yaw,
    required this.zoom,
    required this.nodeCount,
  });

  final List<GalaxyCluster> clusters;
  final double yaw;
  final double zoom;
  final int nodeCount;

  @override
  void paint(Canvas canvas, Size size) {
    final r = size.width / 2;
    final c = Offset(r, r);
    canvas.drawCircle(
      c, r - 2,
      Paint()
        ..color = const Color(0x120A1626)
        ..style = PaintingStyle.fill,
    );
    canvas.drawCircle(
      c, r - 2,
      Paint()
        ..color = const Color(0x334FC3F7)
        ..style = PaintingStyle.stroke
        ..strokeWidth = 1.0,
    );
    var maxDist = 1.0;
    for (final cluster in clusters) {
      maxDist = math.max(maxDist, math.sqrt(
          cluster.center.x * cluster.center.x + cluster.center.z * cluster.center.z));
    }
    final scale = (r - 12) / maxDist;
    for (final cluster in clusters) {
      final ca = math.cos(-yaw), sa = math.sin(-yaw);
      final x = cluster.center.x * ca - cluster.center.z * sa;
      final z = cluster.center.x * sa + cluster.center.z * ca;
      final p = Offset(c.dx + x * scale, c.dy + z * scale);
      final size2 = (2.0 + math.min(4.0, cluster.radius / 60)).toDouble();
      canvas.drawCircle(p, size2 + 3,
          Paint()..color = Color(cluster.tint).withValues(alpha: 0.14)..blendMode = BlendMode.plus);
      canvas.drawCircle(p, size2,
          Paint()..color = Color(cluster.tint).withValues(alpha: 0.85)..blendMode = BlendMode.plus);
    }
    // 当前镜头：中心一个随缩放变化的视野框 + 朝向线
    final view = (r - 12) / math.max(0.45, zoom) * 0.45;
    canvas.drawRect(
      Rect.fromCenter(center: c, width: view * 1.5, height: view),
      Paint()
        ..color = const Color(0x666FE8FF)
        ..style = PaintingStyle.stroke
        ..strokeWidth = 1.0,
    );
    canvas.drawLine(
      c, Offset(c.dx + math.cos(-yaw) * r * 0.8, c.dy + math.sin(-yaw) * r * 0.8),
      Paint()
        ..color = const Color(0x554FC3F7)
        ..strokeWidth = 1.0,
    );
  }

  @override
  bool shouldRepaint(covariant MiniMapPainter old) =>
      old.yaw != yaw || old.zoom != zoom || old.nodeCount != nodeCount;
}
