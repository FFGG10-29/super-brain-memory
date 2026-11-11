import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';

import 'pet_lines.dart';

/// L0 人格意象：大肥鱼桌宠（角色帧与语料取自 QCYTSN/ds-local-pet，本地个人使用）。
///
/// 复刻的交互与动作（对齐桌宠的 manifest 动作协议）：
///   闲置呼吸浮动 / 眨眼 / 左右观察 / 思考 / 侧走遨游 / 说话 / 扫地
///   摸头（长按头） / 被戳（连点三次生气） / 尾巴反应 / 投喂（双击）
///   拖拽 + 重力下落 + 落地眩晕 / 睡觉
///   点击按区域命中：脸头、身体、尾巴；台词气泡 2.6 秒自动消失。
///
/// 人格参数 → 状态与氛围：
///   P 愉悦 → 开心/生气主状态与水色冷暖；A 唤醒 → 微动作频率、气泡速度、低唤醒睡觉
///   D 支配 → 体型与光晕；sassiness → 台词选 sharp 语气；clinginess → 伴飞记忆光点
///   dialect_per_day → 气泡数量；command_channel → 指令通道光环
enum _Act {
  idle, blink, glance, think, walk, talk, happy, angry, eat,
  headPat, poke, tail, drag, fall, land, dizzy, sleep, sweep,
}

const Map<_Act, List<String>> kPetFrames = <_Act, List<String>>{
  _Act.idle: <String>['idle.png'],
  _Act.blink: <String>[
    'blink_0.png', 'blink_1.png', 'blink_2.png', 'blink_3.png',
    'blink_4.png', 'idle.png'
  ],
  _Act.glance: <String>[
    'glance_0.png', 'glance_1.png', 'glance_2.png', 'glance_3.png', 'idle.png'
  ],
  _Act.think: <String>['think.png', 'idle.png'],
  _Act.walk: <String>['walk_0.png', 'walk_1.png', 'walk_2.png', 'walk_3.png'],
  _Act.talk: <String>['talk.png', 'idle.png'],
  _Act.happy: <String>['happy.png', 'happy.png', 'idle.png'],
  _Act.angry: <String>['angry.png', 'angry.png', 'idle.png'],
  _Act.eat: <String>['eat_0.png', 'eat_1.png', 'eat_2.png', 'idle.png'],
  _Act.headPat: <String>[
    'headpat_0.png', 'headpat_1.png', 'headpat_2.png', 'headpat_3.png',
    'headpat_4.png', 'headpat_5.png', 'idle.png'
  ],
  _Act.poke: <String>[
    'poke_0.png', 'poke_1.png', 'poke_2.png', 'poke_3.png', 'idle.png'
  ],
  _Act.tail: <String>[
    'tail_0.png', 'tail_1.png', 'tail_2.png', 'tail_3.png', 'idle.png'
  ],
  _Act.drag: <String>['drag.png'],
  _Act.fall: <String>['falling.png', 'airborne.png'],
  _Act.land: <String>[
    'land_0.png', 'land_1.png', 'land_2.png', 'dizzy.png', 'idle.png'
  ],
  _Act.dizzy: <String>['dizzy.png', 'idle.png'],
  _Act.sleep: <String>[
    'sleep_enter_0.png', 'sleep_enter_1.png', 'sleep_enter_2.png', 'sleep.png'
  ],
  _Act.sweep: <String>['sweep.png', 'idle.png'],
};

double _num(dynamic value, double fallback) =>
    value is num ? value.toDouble() : fallback;

double _cap(double value, double low, double high) =>
    value < low ? low : (value > high ? high : value);

Map<String, double> _emo(Map<String, dynamic> params) {
  final e = params['emotion_baseline'];
  final m = e is Map ? e : <String, dynamic>{};
  return <String, double>{
    'p': _cap(_num(m['p'], 0.2), -1, 1),
    'a': _cap(_num(m['a'], 0.45), -1, 1),
    'd': _cap(_num(m['d'], 0.5), -1, 1),
  };
}

class PersonaWhale extends StatefulWidget {
  const PersonaWhale({
    super.key,
    required this.params,
    this.height = 250,
    this.showStars = true,
    this.sceneMode = false,
    this.size,
    this.roaming = false,
    this.flip = false,
    this.onInteracted,
  });

  final Map<String, dynamic> params;
  final double height;
  final bool showStars;
  /// 场景模式：不画窗口/水色背景，整片星云就是活动空间；由外层按 zoom 缩放。
  final bool sceneMode;
  final double? size;
  /// 自主漫游：外层导航状态机正在移动时切 walk 帧循环（原地交互动作优先）。
  final bool roaming;
  /// 朝向镜像：true 时水平翻转精灵（走路方向变了鱼要掉头）。
  final bool flip;
  /// 用户点击/摸/喂了鱼：外层据此暂停漫游几秒陪玩。
  final VoidCallback? onInteracted;

  @override
  State<PersonaWhale> createState() => _PersonaWhaleState();
}

class _PersonaWhaleState extends State<PersonaWhale>
    with SingleTickerProviderStateMixin {
  late final AnimationController _bg;
  Timer? _frameTimer;
  Timer? _idleTimer;
  Timer? _bubbleTimer;
  Timer? _dropTimer;
  _Act _act = _Act.idle;
  int _frame = 0;
  String? _bubble;
  Offset _drag = Offset.zero;
  int _pokeCount = 0;
  final math.Random _rng = math.Random();

  @override
  void initState() {
    super.initState();
    _bg = AnimationController(
      vsync: this,
      duration: const Duration(seconds: 8),
    )..repeat();
    _scheduleIdle();
  }

  @override
  void didUpdateWidget(covariant PersonaWhale old) {
    super.didUpdateWidget(old);
    if (old.params != widget.params) {
      // 只在情绪跨越阈值时切主状态，避免拖动滑块时反复打断帧动画
      final oldP = _emo(old.params)['p'] ?? 0.2;
      final newP = _emo(widget.params)['p'] ?? 0.2;
      if (oldP <= 0.65 && newP > 0.65) {
        _play(_Act.happy);
      } else if (oldP >= -0.4 && newP < -0.4) {
        _play(_Act.angry);
      }
      final oldA = _emo(old.params)['a'] ?? 0.45;
      final newA = _emo(widget.params)['a'] ?? 0.45;
      if ((oldA - newA).abs() > 0.25) _scheduleIdle();
    }
    if (old.roaming != widget.roaming || old.sceneMode != widget.sceneMode) {
      _syncRoaming();
    }
  }

  @override
  void dispose() {
    _frameTimer?.cancel();
    _idleTimer?.cancel();
    _bubbleTimer?.cancel();
    _dropTimer?.cancel();
    _bg.dispose();
    super.dispose();
  }

  // ── 动作状态机 ───────────────────────────────────────────────────────
  void _play(_Act act, {bool loop = false}) {
    _frameTimer?.cancel();
    if (!mounted) return;
    setState(() {
      _act = act;
      _frame = 0;
    });
    final frames = kPetFrames[act] ?? kPetFrames[_Act.idle]!;
    final ms = (act == _Act.idle || act == _Act.sleep) ? 900 : 165;
    _frameTimer = Timer.periodic(Duration(milliseconds: ms), (timer) {
      if (!mounted) {
        timer.cancel();
        return;
      }
      final next = _frame + 1;
      if (next >= frames.length) {
        if (loop) {
          // 漫游走路帧循环：到尾帧回第 0 帧继续播，直到外层喊停。
          setState(() => _frame = 0);
          return;
        }
        timer.cancel();
        if (act == _Act.sleep) {
          setState(() => _frame = frames.length - 1);
        } else {
          setState(() {
            _act = _Act.idle;
            _frame = 0;
          });
          // 帧播完回到 idle 时，若外层仍在漫游则立即续上走路循环，
          // 避免闲置微动作打断后鱼一边移动却站着不动。
          _syncRoaming();
        }
        return;
      }
      setState(() => _frame = next);
    });
  }

  /// 漫游同步：外层开始移动 → idle 时切 walk 循环；外层停下 → walk 回 idle。
  /// 交互动作（摸头/生气/被戳等）优先，不打断，播完经 idle 自然续上。
  void _syncRoaming() {
    if (!widget.sceneMode) return;
    if (widget.roaming) {
      if (_act == _Act.idle) _play(_Act.walk, loop: true);
    } else {
      if (_act == _Act.walk) _play(_Act.idle);
    }
  }

  /// 闲置微动作分层节奏：眨眼 / 观察 / 偷吃 token / 思考 / 说话 / 散步 / 扫地。
  /// 唤醒度越高节奏越快；低唤醒直接睡着（对齐桌宠的「分层生命节奏」）。
  void _scheduleIdle() {
    _idleTimer?.cancel();
    final a = (_emo(widget.params)['a'] ?? 0.45).abs();
    final period = (5600 - a * 2800).clamp(2400, 6000).toInt();
    _idleTimer = Timer.periodic(Duration(milliseconds: period), (_) {
      if (!mounted) return;
      if ((_emo(widget.params)['a'] ?? 0.45) < -0.55) {
        _play(_Act.sleep);
        _say(kPetInteractionLines['sleep'] ?? <String>[]);
        return;
      }
      if (_act != _Act.idle) return;
      const pool = <_Act>[
        _Act.blink, _Act.blink, _Act.glance, _Act.eat, _Act.think,
        _Act.talk, _Act.walk, _Act.sweep,
      ];
      _play(pool[_rng.nextInt(pool.length)]);
      if (_rng.nextDouble() < 0.32) _say(_toneLines());
    });
  }

  List<String> _toneLines() {
    final sass = _cap(_num(widget.params['sassiness'], 0.6), 0, 1);
    final p = _emo(widget.params)['p'] ?? 0.2;
    final tone = sass > 0.72
        ? 'sharp'
        : (p < -0.2
            ? 'gentle'
            : (_rng.nextDouble() < 0.35 ? 'community' : 'standard'));
    return kPetToneLines[tone] ?? kPetToneLines['standard']!;
  }

  void _say(List<String> pool) {
    final boxSize = widget.sceneMode ? (widget.size ?? widget.height) : widget.height;
    if (pool.isEmpty || boxSize < 100) return;
    final line = pool[_rng.nextInt(pool.length)];
    setState(() => _bubble = line);
    _bubbleTimer?.cancel();
    _bubbleTimer = Timer(const Duration(milliseconds: 2600), () {
      if (mounted) setState(() => _bubble = null);
    });
  }

  // ── 分区交互 ─────────────────────────────────────────────────────────
  void _onTapDown(TapDownDetails d) {
    final width = context.size?.width ?? 240;
    final boxSize = widget.sceneMode ? (widget.size ?? widget.height) : widget.height;
    final lw = boxSize * 0.72;
    final left = (width - lw) / 2;
    final x = ((d.localPosition.dx - left) / lw).clamp(0.0, 1.0).toDouble();
    final y = (d.localPosition.dy / boxSize).clamp(0.0, 1.0).toDouble();
    if (y < 0.36) {
      _hit('head');
    } else if (x > 0.74) {
      _hit('tail');
    } else {
      _hit('body');
    }
  }

  void _hit(String zone) {
    widget.onInteracted?.call();
    if (zone == 'head') {
      _play(_Act.headPat);
      _say(kPetInteractionLines['head'] ?? <String>[]);
      return;
    }
    if (zone == 'tail') {
      _play(_Act.tail);
      _say(kPetInteractionLines['tail'] ?? <String>[]);
      return;
    }
    _pokeCount++;
    if (_pokeCount >= 3) {
      _pokeCount = 0;
      _play(_Act.angry);
      _say(<String>['再戳我真生气了', '哼，不理你了', '爬']);
    } else {
      _play(_Act.poke);
      _say(kPetInteractionLines['body'] ?? <String>[]);
    }
  }

  void _onDoubleTap() {
    widget.onInteracted?.call();
    _play(_Act.eat);
    _say(kPetInteractionLines['feed'] ?? <String>[]);
  }

  void _onLongPress() {
    widget.onInteracted?.call();
    _play(_Act.headPat);
    _say(<String>['摸够没嘛……', '哼，就这一次哦', '头发的结又乱了']);
  }

  void _onPanUpdate(DragUpdateDetails d) {
    _frameTimer?.cancel();
    setState(() {
      _act = _Act.drag;
      _frame = 0;
      _drag += d.delta;
    });
  }

  /// 松手后重力下落 + 落地眩晕（对齐桌宠的抛掷物理，做了轻量简化）。
  void _onPanEnd(DragEndDetails details) {
    _dropTimer?.cancel();
    _play(_Act.fall);
    _say(kPetInteractionLines['drag'] ?? <String>[]);
    var step = 0;
    _dropTimer = Timer.periodic(const Duration(milliseconds: 28), (timer) {
      if (!mounted) {
        timer.cancel();
        return;
      }
      step++;
      setState(() => _drag = Offset(_drag.dx * 0.82, _drag.dy * 0.82 + 3.2));
      if (step >= 12) {
        timer.cancel();
        setState(() => _drag = Offset.zero);
        _play(_Act.land);
      }
    });
  }

  /// 角色精灵：透明背景，只画帧 + 呼吸浮动；场景模式与卡片模式共用。
  Widget _sprite(String frame, double boxSize, Map<String, double> emo,
      double ratio) {
    return AnimatedBuilder(
      animation: _bg,
      builder: (BuildContext context, Widget? child) {
        final t = _bg.value;
        final float = math.sin(t * math.pi * 2) * (3 + emo['a']! * 4);
        final scale = 0.94 + emo['d']! * 0.09;
        return Transform.translate(
          offset: _drag + Offset(math.sin(t * math.pi * 2) * 4, -float),
          child: Transform.scale(
            scale: scale,
            child: Transform.flip(
              flipX: widget.flip,
              child: Image.asset(
                'assets/pet/$frame',
                height: boxSize * ratio,
                fit: BoxFit.contain,
                gaplessPlayback: true,
              ),
            ),
          ),
        );
      },
    );
  }

  Widget _bubbleWidget() => Container(
        constraints: const BoxConstraints(maxWidth: 280),
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
        decoration: BoxDecoration(
          color: const Color(0xE60A1424),
          borderRadius: BorderRadius.circular(10),
          border: Border.all(color: const Color(0x664FC3F7)),
        ),
        child: Text(
          _bubble ?? '',
          textAlign: TextAlign.center,
          maxLines: 2,
          overflow: TextOverflow.ellipsis,
          style: const TextStyle(
              color: Colors.white, fontSize: 10, height: 1.4),
        ),
      );

  @override
  Widget build(BuildContext context) {
    final emo = _emo(widget.params);
    final frames = kPetFrames[_act] ?? kPetFrames[_Act.idle]!;
    final index = _frame < frames.length ? _frame : frames.length - 1;
    final boxSize =
        widget.sceneMode ? (widget.size ?? widget.height) : widget.height;

    // ★ 场景模式：没有小窗口、没有水色背景，整片星云就是她的活动空间。
    //   外层用 Align 定位 + 按 zoom 缩放；这里只保留角色帧与可点击交互，
    //   且不注册 pan，避免吃掉星云的拖拽旋转手势。
    if (widget.sceneMode) {
      return SizedBox(
        width: boxSize,
        height: boxSize,
        child: GestureDetector(
          behavior: HitTestBehavior.deferToChild,
          onTapDown: _onTapDown,
          onDoubleTap: _onDoubleTap,
          onLongPress: _onLongPress,
          child: Stack(
            clipBehavior: Clip.none,
            children: <Widget>[
              Center(child: _sprite(frames[index], boxSize, emo, 0.94)),
              if (_bubble != null)
                Positioned(
                  top: -26,
                  left: -72,
                  right: -72,
                  child: Center(child: _bubbleWidget()),
                ),
            ],
          ),
        ),
      );
    }

    return SizedBox(
      height: widget.height,
      width: double.infinity,
      child: ClipRRect(
        borderRadius: BorderRadius.circular(12),
        child: GestureDetector(
          behavior: HitTestBehavior.opaque,
          onTapDown: _onTapDown,
          onDoubleTap: _onDoubleTap,
          onLongPress: _onLongPress,
          onPanUpdate: _onPanUpdate,
          onPanEnd: _onPanEnd,
          child: Stack(
            children: <Widget>[
              Positioned.fill(
                child: CustomPaint(
                  painter: _PetBackdrop(_bg, widget.params, widget.showStars),
                ),
              ),
              Center(child: _sprite(frames[index], widget.height, emo, 0.84)),
              if (_bubble != null)
                Positioned(
                  top: 6,
                  left: 8,
                  right: 8,
                  child: Center(child: _bubbleWidget()),
                ),
            ],
          ),
        ),
      ),
    );
  }
}

/// 水下氛围：水色 / 波纹 / 气泡 / 记忆星点 / 支配度光晕 / 指令通道光环。
/// 角色本体已换成桌宠真实帧，这里只画背景。
class _PetBackdrop extends CustomPainter {
  _PetBackdrop(this.anim, this.params, this.showStars) : super(repaint: anim);

  final Animation<double> anim;
  final Map<String, dynamic> params;
  final bool showStars;

  @override
  void paint(Canvas canvas, Size size) {
    final t = anim.value;
    final emo = _emo(params);
    final p = emo['p']!;
    final a = emo['a']!;
    final d = emo['d']!;
    final cling = _cap(_num(params['clinginess'], 0.6), 0, 1);
    final dialect = _num(params['dialect_per_day'], 15);
    final command = params['command_channel'] != false;
    final rect = Offset.zero & size;

    final warm = _cap((p + 1) / 2, 0, 1);
    final top = Color.lerp(const Color(0xFF0A3663),
            const Color(0xFF49C9F2), warm) ??
        const Color(0xFF49C9F2);
    final bottom = Color.lerp(const Color(0xFF06182F),
            const Color(0xFF0E7FC4), warm) ??
        const Color(0xFF0E7FC4);
    canvas.drawRect(
      rect,
      Paint()
        ..shader = LinearGradient(
          begin: Alignment.topCenter,
          end: Alignment.bottomCenter,
          colors: <Color>[top, bottom],
        ).createShader(rect),
    );

    final wave = Paint()
      ..color = Colors.white.withValues(alpha: 0.10)
      ..style = PaintingStyle.stroke
      ..strokeWidth = 2;
    for (var i = 0; i < 3; i++) {
      final path = Path();
      final y = 14.0 + i * 13;
      path.moveTo(0, y);
      for (var x = 0.0; x <= size.width; x += 12) {
        path.lineTo(x, y + math.sin(x / 42 + t * math.pi * 2 + i) * 5);
      }
      canvas.drawPath(path, wave);
    }

    final bubbles = _cap(6 + dialect / 3, 6, 22).round();
    final speed = 0.30 + a.abs() * 0.85;
    for (var i = 0; i < bubbles; i++) {
      final seed = i * 0.61803398875;
      final bx = (seed % 1) * size.width;
      final prog = (t * speed + seed) % 1.0;
      final by = size.height - prog * (size.height + 30);
      final r = 2.0 + (i % 4) * 1.9;
      canvas.drawCircle(
        Offset(bx, by),
        r,
        Paint()
          ..color = Colors.white.withValues(alpha: 0.30)
          ..style = PaintingStyle.stroke
          ..strokeWidth = 1.1,
      );
      canvas.drawCircle(
        Offset(bx - r * 0.3, by - r * 0.3),
        r * 0.28,
        Paint()..color = Colors.white.withValues(alpha: 0.45),
      );
    }

    // 记忆星点：7 个主题分区 + 黏人伴飞光点（人格在记忆里遨游）
    if (showStars) {
      final cx = size.width * 0.5;
      final cy = size.height * 0.53;
      final stars = 7 + (cling * 5).round();
      for (var i = 0; i < stars; i++) {
        final ang = i * 2.3999632297 + t * 0.35;
        final base = math.min(size.width, size.height);
        final rad = (i < 7 ? 0.30 + (i % 3) * 0.09 : 0.13 + (i % 3) * 0.05) * base;
        final sx = cx + math.cos(ang) * rad;
        final sy = cy + math.sin(ang) * rad * 0.55;
        final glow = 0.32 + 0.28 * (0.5 + 0.5 * math.sin(t * 6 + i));
        canvas.drawCircle(
          Offset(sx, sy),
          2.2 + (i % 3) * 1.1,
          Paint()..color = const Color(0xFF9BE7FF).withValues(alpha: glow),
        );
      }
    }

    // 支配度光晕（画在角色身后，靠透明度叠加）
    canvas.drawCircle(
      Offset(size.width * 0.5, size.height * 0.53),
      math.min(size.width, size.height) * 0.42,
      Paint()
        ..shader = RadialGradient(colors: <Color>[
          const Color(0xFFBFF0FF).withValues(alpha: 0.10 + (d + 1) * 0.08),
          Colors.transparent,
        ]).createShader(Rect.fromCircle(
          center: Offset(size.width * 0.5, size.height * 0.53),
          radius: math.min(size.width, size.height) * 0.42,
        )),
    );

    if (command) {
      canvas.drawOval(
        Rect.fromCenter(
          center: Offset(size.width * 0.5, size.height * 0.88),
          width: size.width * 0.46,
          height: 18,
        ),
        Paint()
          ..color = const Color(0xFF7FE3FF).withValues(alpha: 0.30)
          ..style = PaintingStyle.stroke
          ..strokeWidth = 1.3,
      );
    }
  }

  @override
  bool shouldRepaint(covariant _PetBackdrop old) =>
      old.params != params || old.showStars != showStars;
}
