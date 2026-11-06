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
  });

  final double focal;
  final double starSize;
  final bool bloom;
  final bool showLabels;
  final bool autoRotate;
  final double spinSpeed;
  final double galaxyThickness;
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

/// GPU 友好渲染：移植 LivingMemory 的 `THREE.Points + AdditiveBlending`。
/// 全部粒子/节点/连线分别用 **一次 Canvas.drawPoints 批量绘制**（约 6 次 draw call），
/// 不做逐节点 blur、不用 MaskFilter；3000 节点 / 24000 尘埃粒子仍可 60fps。
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
  final double burst; // 检索星爆强度 0~1

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

    final cy = math.cos(yaw), sy = math.sin(yaw);
    final cp = math.cos(pitch), sp = math.sin(pitch);
    final cx = size.width / 2, cyy = size.height / 2;

    Projected proj(Vec3 world) => project(
          world, focal: style.focal, cx: cx, cy: cyy, zoom: zoom,
        );

    // 每簇预计算：自转角度 + frame 矩阵 + 旋转后的簇心
    final clusterData = <_ClusterTransform>[];
    for (final cluster in graph.clusters) {
      final a = spinTime * cluster.spin;
      clusterData.add(_ClusterTransform(
        cluster: cluster,
        ca: math.cos(a), sa: math.sin(a),
      ));
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

    // ── ① 背景星尘（一次 drawPoints）───────────────────────────────────
    final starPoints = <Offset>[];
    for (final star in stardust) {
      final layer = star[3].toInt();
      final parallax = 0.35 + layer * 0.32;
      final x = (star[0] + math.sin(yaw * parallax) * 60 * parallax) % size.width;
      final y = (star[1] + math.cos(pitch * parallax) * 40 * parallax) % size.height;
      starPoints.add(Offset(x, y));
    }
    if (starPoints.isNotEmpty) {
      canvas.drawPoints(
        ui.PointMode.points,
        starPoints,
        Paint()
          ..color = const Color(0x558FB4E6)
          ..strokeWidth = 1.4
          ..strokeCap = StrokeCap.round
          ..blendMode = BlendMode.plus,
      );
    }

    // ── ② galaxyDust 粒子云（一次 drawPoints，上限 24000）──────────────
    final dustPoints = <Offset>[];
    final hazePoints = <Offset>[];
    for (final t in clusterData) {
      final dust = t.cluster.dust;
      for (var i = 0; i < dust.length; i++) {
        final p = proj(transform(dust[i], t));
        if (p.x < -80 || p.x > size.width + 80 || p.y < -80 || p.y > size.height + 80) continue;
        final point = Offset(p.x, p.y);
        dustPoints.add(point);
        if (i % 19 == 0 && style.bloom) hazePoints.add(point); // 源码 haze = i%19
      }
    }
    if (dustPoints.isNotEmpty) {
      canvas.drawPoints(
        ui.PointMode.points,
        dustPoints,
        Paint()
          ..color = const Color(0x66B7D6F0)
          ..strokeWidth = 1.15
          ..strokeCap = StrokeCap.round
          ..blendMode = BlendMode.plus,
      );
    }
    if (hazePoints.isNotEmpty) {
      canvas.drawPoints(
        ui.PointMode.points,
        hazePoints,
        Paint()
          ..color = const Color(0x1AE8C79A)
          ..strokeWidth = 9.0
          ..strokeCap = StrokeCap.round
          ..blendMode = BlendMode.plus,
      );
    }

    // ── ③ 节点世界坐标（供边/星爆/脉冲复用）────────────────────────────
    final world = <String, Vec3>{};
    final projected = <String, Projected>{};
    final nodePoints = <Offset>[];
    for (final node in graph.nodes) {
      final t = clusterData.isEmpty
          ? null
          : clusterData[node.cluster.clamp(0, clusterData.length - 1).toInt()];
      final w = t == null ? rotate(node.position, yaw, pitch) : transform(node.position, t);
      final p = proj(w);
      world[node.id] = w;
      projected[node.id] = p;
      if (p.x > -40 && p.x < size.width + 40 && p.y > -40 && p.y < size.height + 40) {
        nodePoints.add(Offset(p.x, p.y));
      }
    }

    // ── ④ 突触边（预算已截断；一次 PointMode.lines）───────────────────
    final linePoints = <Offset>[];
    for (final edge in graph.edges) {
      final a = projected[edge.source], b = projected[edge.target];
      if (a == null || b == null) continue;
      linePoints.add(Offset(a.x, a.y));
      linePoints.add(Offset(b.x, b.y));
    }
    if (linePoints.isNotEmpty) {
      canvas.drawPoints(
        ui.PointMode.lines,
        linePoints,
        Paint()
          ..color = const Color(0x55D2B993)
          ..strokeWidth = 0.85
          ..blendMode = BlendMode.plus,
      );
    }

    // ── ⑤ 检索星爆：焦点 → 命中节点，每个目标一束发散金线（图 3 效果）──
    if (burst > 0 && highlighted.isNotEmpty) {
      final burstLines = <Offset>[];
      final focus = proj(const Vec3(0, 0, 0));
      var seed = 0;
      for (final id in highlighted.take(24)) {
        final target = projected[id];
        if (target == null) continue;
        for (var k = 0; k < 7; k++) {
          seed++;
          final jitter = (hashUnit('$id-$k', seed) - 0.5) * burst * 46;
          burstLines.add(Offset(focus.x, focus.y));
          burstLines.add(Offset(target.x + jitter, target.y + jitter * 0.6));
        }
      }
      if (burstLines.isNotEmpty) {
        canvas.drawPoints(
          ui.PointMode.lines,
          burstLines,
          Paint()
            ..color = Color.fromRGBO(244, 209, 155, 0.22 * burst)
            ..strokeWidth = 0.75
            ..blendMode = BlendMode.plus,
        );
      }
    }

    // ── ⑥ 节点核心（一次 drawPoints）+ 少量高亮光晕/标签 ───────────────
    if (nodePoints.isNotEmpty) {
      if (style.bloom) {
        canvas.drawPoints(
          ui.PointMode.points,
          nodePoints,
          Paint()
            ..color = const Color(0x224FC3F7)
            ..strokeWidth = 8.0 * zoom
            ..strokeCap = StrokeCap.round
            ..blendMode = BlendMode.plus,
        );
      }
      canvas.drawPoints(
        ui.PointMode.points,
        nodePoints,
        Paint()
          ..color = const Color(0xEADFF3FF)
          ..strokeWidth = math.max(1.4, style.starSize * zoom * 0.7)
          ..strokeCap = StrokeCap.round
          ..blendMode = BlendMode.plus,
      );
    }

    var labels = 0;
    for (final id in highlighted) {
      final p = projected[id];
      final node = graph.byId[id];
      if (p == null || node == null) continue;
      canvas.drawCircle(
        Offset(p.x, p.y),
        7.0 * p.scale,
        Paint()
          ..color = const Color(0x99B4EBFF)
          ..style = PaintingStyle.stroke
          ..strokeWidth = 1.1,
      );
      if (style.showLabels && labels < 8) {
        labels++;
        final painter = TextPainter(
          text: TextSpan(text: _short(node.content),
              style: const TextStyle(color: Color(0xFFD7E6F8), fontSize: 10)),
          textDirection: ui.TextDirection.ltr,
        )..layout();
        painter.paint(canvas, Offset(p.x + 10, p.y - 12));
      }
    }

    // ── ⑦ 光脉冲（一次 drawPoints）─────────────────────────────────────
    final pulsePoints = <Offset>[];
    for (final pulse in pulses) {
      final p = proj(rotate(pulse.from.lerp(pulse.to, pulse.progress), yaw, pitch));
      pulsePoints.add(Offset(p.x, p.y));
    }
    if (pulsePoints.isNotEmpty) {
      canvas.drawPoints(
        ui.PointMode.points,
        pulsePoints,
        Paint()
          ..color = const Color(0x55FFFFFF)
          ..strokeWidth = 8.0
          ..strokeCap = StrokeCap.round
          ..blendMode = BlendMode.plus,
      );
      canvas.drawPoints(
        ui.PointMode.points,
        pulsePoints,
        Paint()
          ..color = Colors.white
          ..strokeWidth = 2.6
          ..strokeCap = StrokeCap.round
          ..blendMode = BlendMode.plus,
      );
    }
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
