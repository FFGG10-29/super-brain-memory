import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';

import 'brain_client.dart';
import 'graph_model.dart';
import 'nebula_math.dart';
import 'nebula_painter.dart';

/// 3D 星云页：鼠标拖拽翻滚（yaw/pitch）、滚轮缩放、语义星系簇、WS 光脉冲。
class NebulaPage extends StatefulWidget {
  const NebulaPage({super.key, required this.client});

  final BrainClient client;

  @override
  State<NebulaPage> createState() => NebulaPageState();
}

class NebulaPageState extends State<NebulaPage> with SingleTickerProviderStateMixin {
  late final AnimationController _controller;
  StreamSubscription<Map<String, dynamic>>? _subscription;
  final TextEditingController _search = TextEditingController();

  BrainGraph _graph = BrainGraph(nodes: <BrainNode>[], edges: <BrainEdge>[], clusters: <GalaxyCluster>[]);
  NebulaStyle _style = const NebulaStyle();
  List<List<double>> _stardust = <List<double>>[];
  final List<Pulse3D> _pulses = <Pulse3D>[];
  final Set<String> _highlighted = <String>{};
  final Vec3 _origin = const Vec3(0, 0, 0);

  double _yaw = 0.35;
  double _pitch = -0.22;
  double _zoom = 1.0;
  String _status = '正在连接记忆内核…';
  String _connection = '离线';

  @override
  void initState() {
    super.initState();
    _controller = AnimationController(vsync: this, duration: const Duration(hours: 1))..repeat();
    _controller.addListener(_tick);
    _buildStardust();
    _load();
    widget.client.connectEvents();
    _subscription = widget.client.events.listen(_onEvent);
  }

  void _buildStardust() {
    final rng = math.Random(20261106);
    _stardust = List<List<double>>.generate(900, (i) => <double>[
          rng.nextDouble() * 1600,
          rng.nextDouble() * 1000,
          rng.nextDouble(),
          (i % 3).toDouble(),
        ]);
  }

  Future<void> _load() async {
    try {
      final graph = await widget.client.graph();
      final config = await widget.client.config();
      final nebula = (config['nebula'] as Map<String, dynamic>? ?? <String, dynamic>{});
      if (!mounted) return;
      setState(() {
        _graph = graph;
        _style = NebulaStyle(
          focal: (nebula['focal_length'] as num? ?? 900).toDouble(),
          starSize: (nebula['star_size'] as num? ?? 3.2).toDouble(),
          bloom: nebula['bloom'] != false,
          autoRotate: nebula['auto_rotate'] != false,
          spinSpeed: (nebula['spin_speed'] as num? ?? 0.00035).toDouble(),
        );
        _status = '${graph.nodes.length} 个节点 · ${graph.edges.length} 条突触 · ${graph.clusters.length} 个星系';
      });
    } catch (error) {
      if (mounted) setState(() => _status = '后端未连接：$error');
    }
  }

  void _onEvent(Map<String, dynamic> event) {
    if (!mounted) return;
    final type = event['type']?.toString() ?? '';
    if (type == 'connected') {
      setState(() => _connection = 'WS 已连接');
      return;
    }
    if (type == 'reconnecting') {
      setState(() => _connection = 'WS 重连中');
      return;
    }
    if (type == 'node_created') {
      final node = BrainNode(
        id: event['id']?.toString() ?? event['event_id']?.toString() ?? '',
        content: event['content']?.toString() ?? '',
        memoryType: event['memory_type']?.toString() ?? 'fact',
        tier: event['tier']?.toString() ?? 'buffer',
        isEmotion: event['memory_type']?.toString() == 'emotion',
      );
      _graph.addNode(node);
      _pulses.add(Pulse3D(from: _origin, to: node.position, similarity: 0.85));
      setState(() => _status = '${_graph.nodes.length} 个节点 · 新记忆已点亮');
    } else if (type == 'pulse') {
      final target = _graph.byId[event['target']?.toString() ?? ''];
      if (target != null) {
        _pulses.add(Pulse3D(from: _origin, to: target.position, similarity: 0.7));
      }
    }
  }

  void _tick() {
    if (_pulses.isEmpty && _highlighted.isEmpty) return;
    for (final pulse in _pulses) {
      pulse.step();
    }
    _pulses.removeWhere((pulse) => pulse.done);
    for (final node in _graph.nodes) {
      if (node.flash > 0) node.flash = math.max(0, node.flash - 0.04);
    }
  }

  Future<void> search(String query) async {
    if (query.trim().isEmpty) return;
    try {
      final hits = await widget.client.recall(query);
      if (!mounted) return;
      _highlighted
        ..clear()
        ..addAll(hits.map((hit) => hit.id));
      for (final hit in hits) {
        final node = _graph.byId[hit.id];
        if (node != null) {
          node.flash = 1.0;
          _pulses.add(Pulse3D(from: _origin, to: node.position, similarity: hit.score));
        }
      }
      setState(() => _status = '「$query」命中 ${hits.length} 条 · 光点齐射');
    } catch (error) {
      if (mounted) setState(() => _status = '召回失败：$error');
    }
  }

  void resetView() => setState(() {
        _yaw = 0.35;
        _pitch = -0.22;
        _zoom = 1.0;
        _highlighted.clear();
      });

  @override
  void dispose() {
    _subscription?.cancel();
    _controller
      ..removeListener(_tick)
      ..dispose();
    _search.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Stack(
      children: <Widget>[
        Positioned.fill(
          child: Listener(
            onPointerSignal: (event) {
              if (event is PointerScrollEvent) {
                setState(() => _zoom = (_zoom - event.scrollDelta.dy * 0.0012).clamp(0.35, 3.0));
              }
            },
            child: GestureDetector(
              onPanUpdate: (details) => setState(() {
                _yaw += details.delta.dx * 0.006;
                _pitch = (_pitch + details.delta.dy * 0.006).clamp(-1.35, 1.35);
              }),
              onDoubleTap: resetView,
              child: AnimatedBuilder(
                animation: _controller,
                builder: (context, _) {
                  final seconds = _controller.value * 3600;
                  final yaw = _yaw + (_style.autoRotate ? seconds * 0.0015 : 0);
                  return CustomPaint(
                    painter: NebulaPainter(
                      graph: _graph,
                      yaw: yaw,
                      pitch: _pitch,
                      zoom: _zoom,
                      spinTime: seconds * (_style.spinSpeed / 0.00035),
                      pulses: _pulses,
                      style: _style,
                      highlighted: _highlighted,
                      stardust: _stardust,
                    ),
                    child: Container(),
                  );
                },
              ),
            ),
          ),
        ),
        Positioned(
          left: 18,
          top: 16,
          child: _GlassPanel(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: <Widget>[
                const Text('超级大脑 · 3D 星云', style: TextStyle(color: Color(0xFF7FE9FF), fontSize: 15, fontWeight: FontWeight.w600)),
                const SizedBox(height: 6),
                Text(_status, style: const TextStyle(color: Color(0xFF9FB6D4), fontSize: 11)),
                Text(' $_connection · 拖拽翻滚 · 滚轮缩放 · 双击复位', style: const TextStyle(color: Color(0xFF6B839F), fontSize: 10)),
              ],
            ),
          ),
        ),
        Positioned(
          right: 18,
          top: 16,
          child: _GlassPanel(
            child: SizedBox(
              width: 260,
              child: TextField(
                controller: _search,
                style: const TextStyle(color: Colors.white, fontSize: 12),
                decoration: const InputDecoration(
                  hintText: '检索记忆并激活光脉冲…',
                  hintStyle: TextStyle(color: Color(0xFF63788F), fontSize: 12),
                  border: InputBorder.none,
                  icon: Icon(Icons.search, color: Color(0xFF4FC3F7), size: 18),
                ),
                onSubmitted: search,
              ),
            ),
          ),
        ),
      ],
    );
  }
}

class _GlassPanel extends StatelessWidget {
  const _GlassPanel({required this.child});
  final Widget child;

  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
        decoration: BoxDecoration(
          color: const Color(0xE0061020),
          borderRadius: BorderRadius.circular(10),
          border: Border.all(color: const Color(0xFF1B2A42)),
        ),
        child: child,
      );
}
