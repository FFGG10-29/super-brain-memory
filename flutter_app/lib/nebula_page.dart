import 'dart:async';
import 'dart:math' as math;
import 'dart:ui' as ui;

import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';

import 'brain_client.dart';
import 'graph_model.dart';
import 'nebula_math.dart';
import 'nebula_painter.dart';
import 'persona_whale.dart';
import 'prefs.dart';

/// 3D 星云页：拖拽翻滚、滚轮缩放、语义星团、景深、LOD 与全量渲染。
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

  BrainGraph _full = BrainGraph(nodes: <BrainNode>[], edges: <BrainEdge>[], clusters: <GalaxyCluster>[]);
  BrainGraph _graph = BrainGraph(nodes: <BrainNode>[], edges: <BrainEdge>[], clusters: <GalaxyCluster>[]);
  String _space = '全部';
  NebulaStyle _style = const NebulaStyle();

  /// 星云外观唯一真源：flutter_prefs.json 的 nebula 段。
  Map<String, dynamic> _neb() {
    final value = Prefs.load()['nebula'];
    return value is Map ? Map<String, dynamic>.from(value) : <String, dynamic>{};
  }

  double _nebNum(String key, double fallback) => (_neb()[key] as num?)?.toDouble() ?? fallback;
  bool _nebBool(String key, bool fallback) => _neb()[key] as bool? ?? fallback;

  NebulaStyle _styleFromPrefs() => NebulaStyle(
        focal: _nebNum('focal_length', 900),
        // 星点直径钳制在 1.4~4.5：设置页滑块/旧配置里出现 8.0 这种值，
        // 会让 1261 个节点全变成 9px 光球，屏幕上就只剩「一堆光点」。
        starSize: _nebNum('star_size', 3.2).clamp(1.4, 4.5),
        bloom: _nebBool('bloom', true),
        autoRotate: _nebBool('auto_rotate', true),
        spinSpeed: _nebNum('spin_speed', 0.00035),
        showEdges: _nebBool('show_edges', false),
        showBlackHole: _nebBool('show_black_hole', true),
        showCore: _nebBool('show_core', true),
        twinkle: _nebBool('twinkle', true),
        showGravity: _nebBool('show_gravity', true),
        parallax: _nebBool('parallax', true),
        fullMode: _fullMode,
      );

  void _toggleNeb(String key, bool fallback) {
    final next = _neb()..[key] = !_nebBool(key, fallback);
    Prefs.save(<String, dynamic>{'nebula': next});
    setState(() => _style = _styleFromPrefs());
  }

  Widget _miniToggle(String label, String key, bool fallback) {
    final on = _nebBool(key, fallback);
    return InkWell(
      onTap: () => _toggleNeb(key, fallback),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 3),
        decoration: BoxDecoration(
          color: on ? const Color(0x224FC3F7) : const Color(0x111A2ACC),
          borderRadius: BorderRadius.circular(5),
          border: Border.all(color: on ? const Color(0x664FC3F7) : const Color(0xFF24344C)),
        ),
        child: Text(label,
            style: TextStyle(
                color: on ? const Color(0xFF9FE6FF) : const Color(0xFF6B839F), fontSize: 10)),
      ),
    );
  }

  List<List<double>> _stardust = <List<double>>[];
  final List<Pulse3D> _pulses = <Pulse3D>[];
  final Set<String> _highlighted = <String>{};
  final Vec3 _origin = const Vec3(0, 0, 0);

  double _yaw = 0.35;
  double _pitch = -0.22;
  double _zoom = 1.0;
  double _burst = 0.0;
  double _coreBurst = 0.0;
  String _status = '正在连接记忆内核…';
  String _connection = '离线';

  bool _fullMode = false;
  bool _loading = false;
  int _loadAttempts = 0;
  double _progress = 0.0;
  bool _panelExpanded = true;
  // 大肥鱼显隐交给控制面板（Prefs 持久化），不再是右下角小窗的关闭按钮
  bool get _showWhale => _nebBool('show_whale', true);
  Map<String, dynamic> _personaParams = <String, dynamic>{};

  // ── 大肥鱼自主漫游状态机 ─────────────────────────────────────────────
  // 对齐桌宠的「分层生命节奏」：随机挑目标点 → walk 帧匀速走过去 →
  // 到站休息几秒（ idle 微动作由 PersonaWhale 自己播）→ 再出发。
  // 坐标为 Align 归一化系（-1..1），唤醒度 a 越高走得越勤、越快。
  Offset _petAlign = const Offset(0.35, 0.05);
  Offset? _petTarget;
  bool _petWalking = false;
  bool _petFlip = false;
  Timer? _petRoamTimer;

  @override
  void initState() {
    super.initState();
    _controller = AnimationController(vsync: this, duration: const Duration(hours: 1))..repeat();
    _controller.addListener(_tick);
    _buildStardust();
    // ★ 默认全量渲染：不再用 200 条抽样 + 2-gram 近似牺牲精度；
    //   全量节点分批渐进加载，聚类叠加 namespace 主题先验，跨主题关联边保留。
    _fullMode = Prefs.get<bool>('full_render', true);
    if (_fullMode) {
      _loadAll();
    } else {
      _load();
    }
    widget.client.connectEvents();
    _subscription = widget.client.events.listen(_onEvent);
    _scheduleNextRoam(const Duration(milliseconds: 1800));
    // L0 人格意象取当前可调参数（失败则用默认骨架，不影响星云）
    widget.client.personaGet().then((Map<String, dynamic> d) {
      if (!mounted) return;
      final p = d['params'];
      if (p is Map) {
        setState(() => _personaParams = Map<String, dynamic>.from(p));
      }
    }).catchError((Object _) {});
  }

  void _buildStardust() {
    final rng = math.Random(20261106);
    // 三层：数量与亮度递增，渲染时按层给不同视差速度。
    _stardust = List<List<double>>.generate(1500, (i) => <double>[
          rng.nextDouble() * 1600,
          rng.nextDouble() * 1000,
          rng.nextDouble(),
          (i % 3).toDouble(),
        ]);
  }

  Future<void> _load() async {
    try {
      final graph = await widget.client.graph();
      if (!mounted) return;
      if (graph.nodes.isEmpty) {
        // 后端还没起来 / 数据目录不对：绝不把「空图」当成成功画到屏幕上。
        _loadAttempts++;
        debugPrint('[nebula] _load 空结果（第 $_loadAttempts 次）'
            ' err=${widget.client.lastGraphError}');
        if (_loadAttempts <= 10) {
          setState(() => _status = '等待后端返回记忆节点…（第 $_loadAttempts 次重试）');
          Timer(const Duration(milliseconds: 900), () {
            if (mounted && _full.nodes.isEmpty && !_loading) unawaited(_load());
          });
        } else {
          setState(() => _status = '后端已连接但 graph 返回 0 节点：请检查数据目录 / 后端日志');
        }
        return;
      }
      _loadAttempts = 0;
      debugPrint('[nebula] _load 成功：${graph.nodes.length} 节点，'
          '首个=${graph.nodes.first.id} 坐标范围检查=${_extent(graph)}');
      setState(() {
        _full = graph;
        _graph = graph.filtered(_space);
        _style = _styleFromPrefs();
        _status = '${graph.nodes.length} 节点（默认 200）· ${graph.edges.length} 突触 · '
            '${graph.clusters.length} 星团';
      });
    } catch (error) {
      debugPrint('[nebula] _load 异常：$error');
      if (mounted) setState(() => _status = '后端未连接：$error');
      _loadAttempts++;
      if (_loadAttempts <= 10) {
        Timer(const Duration(milliseconds: 900), () {
          if (mounted && _full.nodes.isEmpty && !_loading) unawaited(_load());
        });
      }
    }
  }

  /// 诊断用：坐标是否全为 0 / NaN / 溢出（肉眼看不见节点的第二类原因）。
  String _extent(BrainGraph graph) {
    var minX = double.infinity, maxX = -double.infinity;
    var bad = 0;
    for (final node in graph.nodes) {
      final x = node.position.x;
      if (x.isNaN || x.isInfinite) bad++;
      if (x < minX) minX = x;
      if (x > maxX) maxX = x;
    }
    return 'x∈[${minX.toStringAsFixed(1)}, ${maxX.toStringAsFixed(1)}] bad=$bad';
  }

  BrainNode _nodeFromRow(Map<String, dynamic> row) {
    final pad = (row['pad'] as List<dynamic>? ?? <dynamic>[0, 0, 0])
        .map((v) => (v as num? ?? 0).toDouble())
        .toList();
    return BrainNode(
      id: row['id']?.toString() ?? '',
      content: row['content']?.toString() ?? '',
      memoryType: row['memory_type']?.toString() ?? 'fact',
      tier: row['tier']?.toString() ?? 'buffer',
      confidence: (row['confidence'] as num? ?? 0.5).toDouble(),
      isEmotion: row['is_emotion'] == true || row['memory_type'] == 'emotion',
      pad: pad,
      namespace: row['namespace']?.toString() ?? 'private',
    );
  }

  /// 全量模式：向后端请求全部记忆，按 500 一批渐进渲染，边收边出图。
  /// 全部到齐后重排成「致密星团 + 悬臂」，并靠 LOD 保住帧率。
  Future<void> _loadAll() async {
    setState(() {
      _loading = true;
      _progress = 0.0;
      _status = '正在向后端请求全量记忆…';
    });
    try {
      final rows = await widget.client.allMemories();
      if (!mounted) return;
      debugPrint('[nebula] 全量模式取到 ${rows.length} 行');
      if (rows.isEmpty) {
        setState(() {
          _loading = false;
          _status = '未取到全量记忆，已保留默认视图';
        });
        await _load();
        return;
      }
      // 主题名映射：星团标签显示空间名而非 topic:<id>
      Map<String, String> topicNames = <String, String>{};
      try {
        final tl = await widget.client.topicsList();
        final list = tl['spaces'] is List<dynamic>
            ? tl['spaces'] as List<dynamic> : <dynamic>[];
        for (final raw in list) {
          if (raw is Map) {
            final id = (raw['id'] ?? '').toString();
            if (id.isNotEmpty) topicNames[id] = (raw['name'] ?? id).toString();
          }
        }
      } catch (_) {}
      // 第一批先建好星团骨架，后续批次只做轻量追加，避免逐批 O(n²) 聚类。
      final first = rows.take(500).toList();
      _full = BrainGraph.fromRows(first, topicNames: topicNames);
      _graph = _full.filtered(_space);
      for (var i = 500; i < rows.length; i += 500) {
        final end = math.min(i + 500, rows.length);
        _full.addNodes(rows.sublist(i, end).map(_nodeFromRow).toList());
        if (!mounted) return;
        setState(() {
          _graph = _full.filtered(_space);
          _progress = end / rows.length;
          _status = '正在加载 $end / ${rows.length} 条记忆节点 · 渲染压力极大…';
        });
        // 让出一帧，保证进度条与新增节点真的被绘制出来。
        await Future<void>.delayed(const Duration(milliseconds: 24));
      }
      _full.recluster(topicNames: topicNames);
      if (!mounted) return;
      setState(() {
        _graph = _full.filtered(_space);
        _style = _styleFromPrefs();
        _loading = false;
        _progress = 1.0;
        _status = '${_graph.nodes.length} 节点 · 全量渲染 · 拉远自动 LOD 保命';
      });
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _status = '全量加载失败：$error';
      });
      await _load();
    }
  }

  void _setFullMode(bool on) {
    Prefs.save(<String, dynamic>{'full_render': on});
    setState(() {
      _fullMode = on;
      _style = _styleFromPrefs();
      _highlighted.clear();
    });
    if (on) {
      unawaited(_loadAll());
    } else {
      unawaited(_load());
    }
  }

  void _onEvent(Map<String, dynamic> event) {
    if (!mounted) return;
    final type = event['type']?.toString() ?? '';
    if (type == 'connected') {
      setState(() => _connection = 'WS 已连接');
      // WS 有时比后端监听端口更晚就绪：首屏那次取数可能已经失败。
      // 连上就把空图重新拉一遍，否则界面会一直停在 0 节点。
      if (_full.nodes.isEmpty && !_loading) {
        debugPrint('[nebula] WS 已连接，重新加载记忆图');
        unawaited(_fullMode ? _loadAll() : _load());
      }
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
      _full.addNode(node);
      _graph = _full.filtered(_space);
      _pulses.add(Pulse3D(from: _origin, to: _graph.worldOf(node), similarity: 0.85));
      _coreBurst = 1.0;
      setState(() => _status = '${_graph.nodes.length} 节点 · 新记忆已点亮');
    } else if (type == 'pulse' || type == 'recall' || type == 'node_accessed') {
      final ids = <String>{};
      final target = event['target']?.toString();
      if (target != null && target.isNotEmpty) ids.add(target);
      final raw = event['hits'] ?? event['ids'];
      if (raw is List) {
        for (final hit in raw) {
          final id = hit is Map ? (hit['id']?.toString() ?? '') : hit.toString();
          if (id.isNotEmpty) ids.add(id);
        }
      }
      var lit = 0;
      for (final id in ids) {
        final node = _graph.byId[id];
        if (node == null) continue;
        node.flash = 1.0;
        _pulses.add(Pulse3D(from: _origin, to: _graph.worldOf(node), similarity: 0.7));
        lit++;
      }
      if (lit > 0 && mounted) {
        _coreBurst = 1.0;
        setState(() => _status = '$lit 条记忆刚被用到 · 中枢爆发');
      }
    }
  }

  double _personaA() {
    final e = _personaParams['emotion_baseline'];
    final a = e is Map ? e['a'] : null;
    return ((a as num?)?.toDouble() ?? 0.45).abs().clamp(0.0, 1.0);
  }

  /// 安排下一次漫游出发：低唤醒鱼懒得动，高唤醒鱼歇一会就想游。
  void _scheduleNextRoam([Duration? delay]) {
    _petRoamTimer?.cancel();
    final a = _personaA();
    final wait =
        (2600 + math.Random().nextInt(3800) - a * 1800).clamp(1200, 7200).toInt();
    _petRoamTimer = Timer(delay ?? Duration(milliseconds: wait), () {
      if (!mounted) return;
      final rng = math.Random();
      setState(() {
        _petTarget = Offset(
          -0.72 + rng.nextDouble() * 1.44,
          -0.60 + rng.nextDouble() * 1.20,
        );
        _petWalking = true;
      });
    });
  }

  /// 每帧推进漫游（由 AnimatedBuilder 驱动，~60fps，按 1/60s 步进足够平滑）。
  void _tickPet() {
    if (!_petWalking) return;
    final target = _petTarget;
    if (target == null) {
      _petWalking = false;
      return;
    }
    final a = _personaA();
    final delta = target - _petAlign;
    final dist = delta.distance;
    // 归一化速度：走完一整屏约 9~16 秒，唤醒度高时明显更快。
    final speedPerSec = 0.10 + a * 0.12;
    final step = speedPerSec / 60;
    if (dist <= step) {
      _petAlign = target;
      _petWalking = false;
      _petTarget = null;
      _scheduleNextRoam();
    } else {
      _petAlign = _petAlign + delta / dist * step;
      _petFlip = delta.dx > 0;
    }
  }

  /// 用户点了/摸了/喂了鱼：停下游动陪玩，几秒后再继续漫游。
  void _onPetInteracted() {
    _petRoamTimer?.cancel();
    _petWalking = false;
    _petTarget = null;
    _scheduleNextRoam(const Duration(milliseconds: 5200));
  }

  void _tick() {
    for (final pulse in _pulses) {
      pulse.step();
    }
    _pulses.removeWhere((pulse) => pulse.done);
    for (final node in _graph.nodes) {
      if (node.flash > 0) node.flash = math.max(0, node.flash - 0.012);
    }
    if (_burst > 0) _burst = math.max(0, _burst - 0.004);
    if (_coreBurst > 0) _coreBurst = math.max(0, _coreBurst - 0.012);
    _tickPet();
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
          _pulses.add(Pulse3D(from: _origin, to: _graph.worldOf(node), similarity: hit.score));
        }
      }
      setState(() {
        _burst = 1.0;
        _coreBurst = 1.0;
        _status = '「$query」命中 ${hits.length} 条 · 中枢爆发 + 星爆齐射';
      });
    } catch (error) {
      if (mounted) setState(() => _status = '召回失败：$error');
    }
  }

  void resetView() => setState(() {
        _yaw = 0.35;
        _pitch = -0.22;
        _zoom = 1.0;
        _highlighted.clear();
        _burst = 0.0;
      });

  void applySpace(String space) => setState(() {
        _space = space;
        _graph = _full.filtered(space);
        _highlighted.clear();
        _yaw = 0.35;
        _pitch = -0.22;
        _zoom = 1.0;
        _burst = 0.0;
      });

  @override
  void dispose() {
    _petRoamTimer?.cancel();
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
                  // ★ 整片星云就是大肥鱼的活动空间：自主漫游状态机挑目标点，
                  //   walk 帧游过去、到站休息、再出发；交互时停下陪玩。
                  //   原先那条 1 小时周期的 Lissajous 轨迹慢得像静止，已废弃。
                  final petSize = (128 * _zoom).clamp(56.0, 340.0).toDouble();
                  return Stack(
                    children: <Widget>[
                      Positioned.fill(
                        child: CustomPaint(
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
                            burst: _burst,
                            coreBurst: _coreBurst,
                          ),
                          child: Container(),
                        ),
                      ),
                      if (_showWhale)
                        Align(
                          alignment: Alignment(_petAlign.dx, _petAlign.dy),
                          child: SizedBox(
                            width: petSize,
                            height: petSize,
                            child: PersonaWhale(
                              params: _personaParams,
                              sceneMode: true,
                              size: petSize,
                              showStars: false,
                              roaming: _petWalking,
                              flip: _petFlip,
                              onInteracted: _onPetInteracted,
                            ),
                          ),
                        ),
                    ],
                  );
                },
              ),
            ),
          ),
        ),

        // ── 左上：半透明毛玻璃悬浮控制面板（可折叠）────────────────────
        Positioned(
          left: 18,
          top: 16,
          child: _GlassPanel(
            child: AnimatedSize(
              duration: const Duration(milliseconds: 180),
              alignment: Alignment.topLeft,
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: <Widget>[
                  Row(mainAxisSize: MainAxisSize.min, children: <Widget>[
                    const Text('超级大脑 · 3D 星云',
                        style: TextStyle(color: Color(0xFF7FE9FF), fontSize: 13,
                            fontWeight: FontWeight.w600)),
                    const SizedBox(width: 8),
                    InkWell(
                      onTap: () => setState(() => _panelExpanded = !_panelExpanded),
                      child: Icon(
                        _panelExpanded ? Icons.unfold_less : Icons.unfold_more,
                        size: 15, color: const Color(0xFF6B839F),
                      ),
                    ),
                  ]),
                  const SizedBox(height: 5),
                  Text(_status, style: const TextStyle(color: Color(0xFF9FB6D4), fontSize: 10)),
                  if (_loading) ...<Widget>[
                    const SizedBox(height: 6),
                    SizedBox(
                      width: 250,
                      height: 3,
                      child: LinearProgressIndicator(
                        value: _progress,
                        backgroundColor: const Color(0xFF1B2A42),
                        valueColor: const AlwaysStoppedAnimation<Color>(Color(0xFF4FC3F7)),
                      ),
                    ),
                  ],
                  if (_panelExpanded) ...<Widget>[
                    const SizedBox(height: 4),
                    Text('$_connection · 拖拽翻滚 · 滚轮缩放 · 双击复位',
                        style: const TextStyle(color: Color(0xFF6B839F), fontSize: 9)),
                    const SizedBox(height: 7),
                    InkWell(
                      onTap: () => _setFullMode(!_fullMode),
                      child: Container(
                        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 5),
                        decoration: BoxDecoration(
                          color: _fullMode ? const Color(0x33FF9A5B) : const Color(0x140A1B2E),
                          borderRadius: BorderRadius.circular(6),
                          border: Border.all(
                              color: _fullMode ? const Color(0x88FF9A5B) : const Color(0x442F4A6B)),
                        ),
                        child: Row(mainAxisSize: MainAxisSize.min, children: <Widget>[
                          Icon(_fullMode ? Icons.radio_button_checked : Icons.radio_button_off,
                              size: 12,
                              color: _fullMode
                                  ? const Color(0xFFFFC08A)
                                  : const Color(0xFF6B839F)),
                          const SizedBox(width: 6),
                          Text('全量渲染模式 (性能警告)',
                              style: TextStyle(
                                  fontSize: 10,
                                  color: _fullMode
                                      ? const Color(0xFFFFD2AC)
                                      : const Color(0xFF9FB6D4))),
                        ]),
                      ),
                    ),
                    const SizedBox(height: 7),
                    SizedBox(
                      width: 268,
                      child: Wrap(spacing: 5, runSpacing: 5, children: <Widget>[
                        _miniToggle('突触连线', 'show_edges', false),
                        _miniToggle('辉光', 'bloom', true),
                        _miniToggle('呼吸闪烁', 'twinkle', true),
                        _miniToggle('中枢', 'show_core', true),
                        _miniToggle('黑洞', 'show_black_hole', true),
                        _miniToggle('引力线', 'show_gravity', true),
                        _miniToggle('视差星尘', 'parallax', true),
                        _miniToggle('自动旋转', 'auto_rotate', true),
                        _miniToggle('大肥鱼', 'show_whale', true),
                      ]),
                    ),
                    // 主题星团：显示每团的主导 namespace 与占比（聚类先验，不是硬隔离）
                    if (_graph.clusters.isNotEmpty) ...<Widget>[
                      const SizedBox(height: 7),
                      SizedBox(
                        width: 268,
                        child: Wrap(
                          spacing: 5,
                          runSpacing: 4,
                          children: <Widget>[
                            for (var i = 0;
                                i < _graph.clusters.length && i < 12;
                                i++)
                              Container(
                                padding: const EdgeInsets.symmetric(
                                    horizontal: 6, vertical: 3),
                                decoration: BoxDecoration(
                                  color: Color(_graph.clusters[i].tint)
                                      .withValues(alpha: 0.12),
                                  borderRadius: BorderRadius.circular(4),
                                ),
                                child: Text(
                                  _graph.clusters[i].label.isEmpty
                                      ? '星团 ${i + 1}'
                                      : _graph.clusters[i].label,
                                  style: TextStyle(
                                    color: Color(_graph.clusters[i].tint),
                                    fontSize: 9,
                                  ),
                                ),
                              ),
                          ],
                        ),
                      ),
                    ],
                    const SizedBox(height: 6),
                    const SizedBox(
                      width: 268,
                      child: Text(
                        '近处节点大而亮、远处小而暗偏红/蓝；镜头拉远自动关连线与标签。'
                        '中枢＝刚用过的记忆，黑洞＝久未使用，被召回即闪金光。',
                        style: TextStyle(color: Color(0xFF4E6A8A), fontSize: 9, height: 1.5),
                      ),
                    ),
                  ],
                ],
              ),
            ),
          ),
        ),

        // ── 右上：检索 ──────────────────────────────────────────────────
        Positioned(
          right: 18,
          top: 16,
          child: _GlassPanel(
            child: SizedBox(
              width: 240,
              child: TextField(
                controller: _search,
                style: const TextStyle(color: Colors.white, fontSize: 12),
                decoration: const InputDecoration(
                  hintText: '检索记忆并激活光脉冲…',
                  hintStyle: TextStyle(color: Color(0xFF63788F), fontSize: 11),
                  border: InputBorder.none,
                  icon: Icon(Icons.search, color: Color(0xFF4FC3F7), size: 17),
                ),
                onSubmitted: search,
              ),
            ),
          ),
        ),

        // ── 右下：小地图 / 雷达 ─────────────────────────────────────────
        Positioned(
          right: 18,
          bottom: 18,
          child: _GlassPanel(
            padding: 8,
            child: Column(mainAxisSize: MainAxisSize.min, children: <Widget>[
              const Text('全局星图',
                  style: TextStyle(color: Color(0xFF6B839F), fontSize: 9)),
              const SizedBox(height: 4),
              SizedBox(
                width: 104,
                height: 104,
                child: CustomPaint(
                  painter: MiniMapPainter(
                    clusters: _graph.clusters,
                    yaw: _yaw,
                    zoom: _zoom,
                    nodeCount: _graph.nodes.length,
                  ),
                ),
              ),
            ]),
          ),
        ),
      ],
    );
  }
}

/// 半透明毛玻璃悬浮面板：背景高斯模糊 + 低不透明度底色。
class _GlassPanel extends StatelessWidget {
  const _GlassPanel({required this.child, this.padding = 12});
  final Widget child;
  final double padding;

  @override
  Widget build(BuildContext context) => ClipRRect(
        borderRadius: BorderRadius.circular(12),
        child: BackdropFilter(
          filter: ui.ImageFilter.blur(sigmaX: 16, sigmaY: 16),
          child: Container(
            padding: EdgeInsets.all(padding),
            decoration: BoxDecoration(
              color: const Color(0x700A1526),
              borderRadius: BorderRadius.circular(12),
              border: Border.all(color: const Color(0x334FC3F7)),
              boxShadow: const <BoxShadow>[
                BoxShadow(color: Color(0x33000000), blurRadius: 18, offset: Offset(0, 6)),
              ],
            ),
            child: child,
          ),
        ),
      );
}
