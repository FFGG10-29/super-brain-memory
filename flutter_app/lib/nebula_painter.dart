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
  Vec3 from;
  Vec3 to;
  final double similarity;
  double progress = 0.0;
  bool get done => progress >= 1.0;
  void step() => progress += 0.012 + similarity * 0.018;
}

/// 真 3D 星云：旋转矩阵 + 透视投影 + 按深度排序 + 加色光晕（bloom 近似）。
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

  Projected _project(Vec3 p, Size size) => project(
        p,
        focal: style.focal,
        cx: size.width / 2,
        cy: size.height / 2,
        zoom: zoom,
      );

  Vec3 _world(BrainNode node) {
    if (node.cluster < 0 || node.cluster >= graph.clusters.length) return rotate(node.position, yaw, pitch);
    final cluster = graph.clusters[node.cluster];
    // spinTime 单位=秒；cluster.spin 是每秒弧度的缩放系数（星系盘缓慢自转）
    final spun = spinAround(node.position, cluster.center, spinTime * cluster.spin * 8);
    return rotate(spun, yaw, pitch);
  }

  @override
  void paint(Canvas canvas, Size size) {
    final rect = Offset.zero & size;
    canvas.drawRect(
      rect,
      Paint()
        ..shader = const RadialGradient(
          center: Alignment(0, -0.12),
          radius: 1.25,
          colors: <Color>[Color(0xFF0B1730), Color(0xFF05080E), Color(0xFF020307)],
          stops: <double>[0.0, 0.55, 1.0],
        ).createShader(rect),
    );

    _drawStardust(canvas, size);

    final positions = <String, Projected>{};
    final world = <String, Vec3>{};
    for (final node in graph.nodes) {
      final p = _world(node);
      world[node.id] = p;
      positions[node.id] = _project(p, size);
    }

    _drawEdges(canvas, positions, world);
    _drawNodes(canvas, size, positions, world);
    _drawPulses(canvas, size);
  }

  void _drawStardust(Canvas canvas, Size size) {
    for (final star in stardust) {
      final layer = star[3].toInt();
      final parallax = 0.35 + layer * 0.32;
      final x = (star[0] + math.sin(yaw * parallax) * 60 * parallax) % size.width;
      final y = (star[1] + math.cos(pitch * parallax) * 40 * parallax) % size.height;
      final alpha = (0.10 + star[2] * 0.42).clamp(0.0, 1.0);
      canvas.drawCircle(
        Offset(x, y),
        0.5 + star[2] * 1.5,
        Paint()..color = Color.fromRGBO(150, 190, 255, alpha),
      );
    }
  }

  void _drawEdges(Canvas canvas, Map<String, Projected> positions, Map<String, Vec3> world) {
    for (final edge in graph.edges) {
      final a = positions[edge.source], b = positions[edge.target];
      if (a == null || b == null) continue;
      final depth = (world[edge.source]!.z + world[edge.target]!.z) / 2;
      final fade = (1 - depth / (style.focal * 1.8)).clamp(0.08, 1.0);
      final alpha = (0.10 + edge.weight * 0.45) * fade;
      canvas.drawLine(
        Offset(a.x, a.y),
        Offset(b.x, b.y),
        Paint()
          ..color = Color.fromRGBO(110, 205, 255, alpha)
          ..strokeWidth = (0.7 + edge.weight * 1.6) * ((a.scale + b.scale) / 2)
          ..blendMode = BlendMode.plus,
      );
    }
  }

  void _drawNodes(Canvas canvas, Size size, Map<String, Projected> positions, Map<String, Vec3> world) {
    final ordered = List<BrainNode>.from(graph.nodes)
      ..sort((a, b) => (world[b.id]?.z ?? 0).compareTo(world[a.id]?.z ?? 0));
    final labels = <_Label>[];

    for (final node in ordered) {
      final p = positions[node.id];
      if (p == null) continue;
      final base = Color(node.colorValue);
      final radius = style.starSize * p.scale * (node.isEmotion ? 1.7 : 1.0) * (1 + node.flash * 0.8);
      final depthFade = (1 - world[node.id]!.z / (style.focal * 2.2)).clamp(0.25, 1.0);
      final isHot = highlighted.contains(node.id);

      if (style.bloom) {
        canvas.drawCircle(
          Offset(p.x, p.y),
          radius * 3.4,
          Paint()
            ..color = base.withOpacity((0.10 + node.flash * 0.22) * depthFade)
            ..maskFilter = MaskFilter.blur(BlurStyle.normal, radius * 2.1)
            ..blendMode = BlendMode.plus,
        );
      }
      canvas.drawCircle(
        Offset(p.x, p.y),
        radius * 1.75,
        Paint()
          ..color = base.withOpacity(0.20 * depthFade)
          ..blendMode = BlendMode.plus,
      );
      canvas.drawCircle(
        Offset(p.x, p.y),
        radius,
        Paint()
          ..color = (node.flash > 0.3 ? Colors.white : base).withOpacity((0.75 + 0.25 * depthFade))
          ..blendMode = BlendMode.plus,
      );
      if (isHot) {
        canvas.drawCircle(
          Offset(p.x, p.y),
          radius * 2.6,
          Paint()
            ..color = const Color(0xFFB4EBFF).withOpacity(0.85)
            ..style = PaintingStyle.stroke
            ..strokeWidth = 1.2,
        );
        labels.add(_Label(node, p));
      }
    }

    if (!style.showLabels) return;
    final selected = labels.take(8).toList();
    for (final item in selected) {
      final painter = TextPainter(
        text: TextSpan(
          text: _short(item.node.content),
          style: const TextStyle(color: Color(0xFFBFD8F5), fontSize: 10),
        ),
        textDirection: ui.TextDirection.ltr,
      )..layout();
      painter.paint(canvas, Offset(item.p.x + item.p.scale * 9, item.p.y - 12));
    }
  }

  void _drawPulses(Canvas canvas, Size size) {
    for (final pulse in pulses) {
      final point = rotate(pulse.from.lerp(pulse.to, pulse.progress), yaw, pitch);
      final p = _project(point, size);
      final radius = 4.5 * p.scale;
      canvas.drawCircle(
        Offset(p.x, p.y),
        radius * 3.2,
        Paint()
          ..color = const Color(0xFF7FE9FF).withOpacity(0.22)
          ..maskFilter = MaskFilter.blur(BlurStyle.normal, radius * 2)
          ..blendMode = BlendMode.plus,
      );
      canvas.drawCircle(
        Offset(p.x, p.y),
        radius,
        Paint()
          ..color = Colors.white
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

class _Label {
  const _Label(this.node, this.p);
  final BrainNode node;
  final Projected p;
}
