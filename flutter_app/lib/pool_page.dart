import 'dart:async';

import 'package:flutter/material.dart';

import 'brain_client.dart';

/// 号池网关 —— 对齐 dsh-xdpool 原版：账号池表格（账号/区域/状态/已服务/操作）+
/// 重新扫描凭证 + 一键签到 + 模型目录（WorkBuddy 倍率/上下文/限时免费/开关）+
/// 云供应商 API key 配置 + 拉起 WorkBuddy 登录（免密 lane 已移除）。
/// 全部走后端真实 RPC：pool.status/rescan/logout/restore/catalog/
/// catalog_refresh/model_toggle/checkin/login_start。
class PoolPage extends StatefulWidget {
  const PoolPage({super.key, required this.client});

  final BrainClient client;

  @override
  State<PoolPage> createState() => _PoolPageState();
}

class _PoolPageState extends State<PoolPage> {
  static const Color _bg = Color(0xFF0B1220);
  static const Color _panel = Color(0xFF121C2E);
  static const Color _border = Color(0xFF22334D);
  static const Color _hint = Color(0xFF7C93B2);
  static const Color _ok = Color(0xFF4ADE80);
  static const Color _warn = Color(0xFFFACC15);
  static const Color _dead = Color(0xFFF87171);
  static const Color _accent = Color(0xFF6EE7B7);
  static const Color _promo = Color(0xFFF472B6);

  /// 实测拒绝 reasoning_effort=off 的模型（off-thinking.ts issue #34），硬墙。
  static const Set<String> _refusesOff = <String>{
    'deepseek-v4.1-flash', 'deepseek-v4.1-flash-sg', 'deepseek-v4-pro',
    'primary-model', 'gpt-6-astra', 'gpt-5.6-sol', 'gpt-5.6-terra',
    'gpt-5.6-luna', 'gemini-3.5-flash',
  };
  static const List<String> _thinkingLevels = <String>[
    'minimal', 'low', 'medium', 'high', 'xhigh', 'max',
  ];

  Map<String, dynamic> _data = <String, dynamic>{};
  Map<String, dynamic> _catalog = <String, dynamic>{};
  List<Map<String, dynamic>> _registry = <Map<String, dynamic>>[];
  // 已禁用模型一律隐藏（关掉就消失）；要恢复去「同步模型表」后重新开启或后端 accounts.json 改
  String _message = '';
  bool _busy = false;
  String _acting = '';
  // ★ 浏览器 OAuth 扫码登录（复用 wbmod 号池网关闭环，凭证落共享 vault 自动入池）
  Timer? _oauthPollTimer;
  String _oauthLoginId = '';
  int _oauthElapsedSec = 0;

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  @override
  void dispose() {
    _oauthPollTimer?.cancel();
    super.dispose();
  }

  /// 浏览器扫码登录：start 拿 authUrl（后端已用系统浏览器打开），轮询 poll 直至扫码成功。
  Future<void> _oauthLogin() async {
    if (_acting.isNotEmpty) return;
    setState(() {
      _acting = 'oauth';
      _message = '正在发起浏览器登录…';
      _oauthElapsedSec = 0;
    });
    try {
      final start = await widget.client.call('pool.oauth_start', <String, dynamic>{'region': 'cn'});
      if (start is! Map || start['ok'] != true) {
        throw Exception(start is Map ? (start['error'] ?? '发起失败') : '发起失败');
      }
      _oauthLoginId = start['loginId'].toString();
      final authUrl = start['authUrl'].toString();
      if (mounted) setState(() {
        _message = start['opened'] == true
            ? '浏览器已打开，请扫码确认（最长等 5 分钟）…\n若未弹出，请手动打开：$authUrl'
            : '请手动在浏览器打开并扫码：$authUrl';
      });
      _oauthPollTimer?.cancel();
      _oauthPollTimer = Timer.periodic(const Duration(seconds: 2), (_) async {
        _oauthElapsedSec += 2;
        try {
          final raw = await widget.client.call('pool.oauth_poll',
              <String, dynamic>{'loginId': _oauthLoginId});
          final Map<String, dynamic> p = raw is Map
              ? Map<String, dynamic>.from(raw) : <String, dynamic>{};
          final done = p['done'] == true;
          final ok = p['ok'] == true;
          if (done && ok) {
            _oauthPollTimer?.cancel();
            final account = p['account'] is Map
                ? Map<String, dynamic>.from(p['account'] as Map) : <String, dynamic>{};
            if (mounted) setState(() {
              _acting = '';
              _message = '登录成功，账号已入池：${(account['label'] ?? account['id'] ?? '')}';
            });
            await _load();
            return;
          }
          if (done && !ok) {
            _oauthPollTimer?.cancel();
            if (mounted) setState(() {
              _acting = '';
              _message = '登录未完成：${p['error'] ?? '超时'}';
            });
            return;
          }
          if (_oauthElapsedSec >= 150) {
            _oauthPollTimer?.cancel();
            if (mounted) setState(() {
              _acting = '';
              _message = '扫码超时（5 分钟），请重新发起';
            });
            return;
          }
          if (mounted) setState(() => _message = '等待扫码确认… $_oauthElapsedSec/150 秒');
        } catch (_) { /* 网络抖动继续轮询 */ }
      });
    } catch (e) {
      if (mounted) setState(() {
        _acting = '';
        _message = '浏览器登录失败：$e';
      });
    }
  }

  Future<void> _load({bool forceCatalog = false}) async {
    setState(() => _busy = true);
    try {
      final status = await widget.client.poolStatus();
      Map<String, dynamic> catalog = <String, dynamic>{};
      try {
        final result = await widget.client.call('pool.catalog',
            <String, dynamic>{'force': forceCatalog});
        if (result is Map<String, dynamic>) catalog = result;
      } catch (_) {}
      if (_registry.isEmpty) {
        try {
          final reg = await widget.client.call('pool.provider_registry', <String, dynamic>{});
          if (reg is Map && reg['ok'] == true) {
            _registry = (reg['providers'] as List<dynamic>? ?? <dynamic>[])
                .whereType<Map>()
                .map(Map<String, dynamic>.from)
                .toList();
          }
        } catch (_) {}
      }
      if (!mounted) return;
      setState(() {
        _data = status;
        _catalog = catalog;
        _message = '';
      });
    } catch (error) {
      if (mounted) setState(() => _message = '加载失败：$error');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _rpc(String method, Map<String, dynamic> args, String tag,
      {String? okMessage}) async {
    if (_acting.isNotEmpty) return;
    setState(() => _acting = tag);
    try {
      final result = await widget.client.call(method, args);
      if (!mounted) return;
      if (result is Map && result['ok'] == true) {
        if (method.startsWith('pool.catalog')) {
          setState(() {
            _catalog = Map<String, dynamic>.from(result);
            _message = '';
          });
        } else {
          setState(() => _message = okMessage ?? '');
          await _load();
        }
        if (okMessage != null && method != 'pool.catalog') {
          setState(() => _message = okMessage);
        }
      } else {
        setState(() => _message =
            '$method 失败：${result is Map ? (result['error'] ?? '未知原因') : '响应异常'}');
      }
    } catch (error) {
      if (mounted) setState(() => _message = '$method 失败：$error');
    } finally {
      if (mounted) setState(() => _acting = '');
    }
  }

  Future<void> _toggleModel(String modelId, bool enabled) async {
    // 乐观更新：开关立即变，不打断交互（后台失败才回滚提示）
    final disabledList = ((_catalog['disabled_models'] as List<dynamic>? ?? <dynamic>[]))
        .map((e) => e.toString())
        .toSet();
    if (enabled) {
      disabledList.remove(modelId);
    } else {
      disabledList.add(modelId);
    }
    setState(() {
      _catalog['disabled_models'] = disabledList.toList();
      _message = enabled ? '已启用 $modelId' : '已禁用 $modelId';
    });
    try {
      final result = await widget.client
          .call('pool.model_toggle', <String, dynamic>{'model_id': modelId, 'enabled': enabled});
      if (!mounted) return;
      if (!(result is Map && result['ok'] == true)) {
        // 失败回滚
        final rollback = ((_catalog['disabled_models'] as List<dynamic>? ?? <dynamic>[]))
            .map((e) => e.toString())
            .toSet();
        if (enabled) {
          rollback.add(modelId);
        } else {
          rollback.remove(modelId);
        }
        setState(() {
          _catalog['disabled_models'] = rollback.toList();
          _message = '开关失败：${result is Map ? (result['error'] ?? '未知原因') : '响应异常'}';
        });
      }
    } catch (error) {
      if (mounted) setState(() => _message = '开关失败：$error');
    }
  }

  /// 同名同倍率的一组路由变体（hy3/hy3-b/hy3-c…）统一启停：
  /// 一次性改 disabled_models 并批量告知后端，避免逐条 RPC 与中间态闪烁。
  Future<void> _toggleModelGroup(List<String> ids, bool enabled) async {
    final disabledList = ((_catalog['disabled_models'] as List<dynamic>? ?? <dynamic>[]))
        .map((e) => e.toString())
        .toSet();
    for (final id in ids) {
      if (enabled) {
        disabledList.remove(id);
      } else {
        disabledList.add(id);
      }
    }
    setState(() {
      _catalog['disabled_models'] = disabledList.toList();
      _acting = 'group-${ids.first}';
      _message = enabled ? '已启用' : '已禁用';
    });
    try {
      for (final id in ids) {
        final result = await widget.client.call(
            'pool.model_toggle', <String, dynamic>{'model_id': id, 'enabled': enabled});
        if (result is! Map || result['ok'] != true) {
          throw (result is Map ? (result['error'] ?? '未知原因') : '响应异常');
        }
      }
    } catch (error) {
      // 失败回滚整组
      final rollback = ((_catalog['disabled_models'] as List<dynamic>? ?? <dynamic>[]))
          .map((e) => e.toString())
          .toSet();
      for (final id in ids) {
        if (enabled) {
          rollback.add(id);
        } else {
          rollback.remove(id);
        }
      }
      if (mounted) {
        setState(() {
          _catalog['disabled_models'] = rollback.toList();
          _message = '分组开关失败：$error';
        });
      }
    } finally {
      if (mounted) setState(() => _acting = '');
    }
  }

  Widget _pill(String text, Color tone) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
        decoration: BoxDecoration(
          color: tone.withValues(alpha: 0.14),
          borderRadius: BorderRadius.circular(999),
          border: Border.all(color: tone.withValues(alpha: 0.5)),
        ),
        child: Text(text, style: TextStyle(color: tone, fontSize: 10)),
      );

  Widget _actionButton(String label, IconData icon, String tag, VoidCallback onTap,
      {Color tone = _accent}) {
    final acting = _acting == tag;
    return TextButton.icon(
      onPressed: _acting.isNotEmpty ? null : onTap,
      icon: acting
          ? const SizedBox(width: 12, height: 12,
              child: CircularProgressIndicator(strokeWidth: 2, color: _accent))
          : Icon(icon, size: 14, color: tone),
      label: Text(label, style: TextStyle(color: tone, fontSize: 12)),
      style: TextButton.styleFrom(
        backgroundColor: tone.withValues(alpha: 0.1),
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
        minimumSize: Size.zero,
        tapTargetSize: MaterialTapTargetSize.shrinkWrap,
        shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(6),
            side: BorderSide(color: tone.withValues(alpha: 0.4))),
      ),
    );
  }

  // 账号行：● 账号 | 区域 | 状态 | 已服务 | 操作（退出登录/恢复）
  Widget _accountRow(Map account) {
    final id = (account['id'] ?? '').toString();
    final label = (account['label'] ?? id).toString();
    final region = (account['region'] ?? '').toString();
    final ignored = (account['ignored'] ?? false) == true;
    final dead = (account['dead'] ?? false) == true;
    final deadSec = (account['dead_remaining_sec'] ?? 0) as int;
    final served = (account['served'] ?? 0) as int;
    final cooldowns = (account['model_cooldowns'] as Map<dynamic, dynamic>? ?? {});
    final status = ignored
        ? _pill('已移出', _hint)
        : dead
            ? _pill('失效 ${deadSec}s', _dead)
            : (cooldowns.isNotEmpty
                ? _pill('冷却中', _warn)
                : _pill('ready', _ok));
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 7),
      child: Row(children: <Widget>[
        Container(
          width: 8, height: 8,
          decoration: BoxDecoration(
              shape: BoxShape.circle,
              color: ignored ? _hint : (dead ? _dead : _ok)),
        ),
        const SizedBox(width: 8),
        Expanded(
          flex: 4,
          child: Text(label,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(
                  color: ignored ? _hint : Colors.white, fontSize: 12)),
        ),
        Expanded(
          flex: 2,
          child: Text(region.isEmpty ? '-' : region,
              style: const TextStyle(color: _hint, fontSize: 11)),
        ),
        Expanded(
          flex: 4,
          child: Align(alignment: Alignment.centerLeft, child: status),
        ),
        Expanded(
          flex: 2,
          child: Text('已服务 $served',
              style: const TextStyle(color: _hint, fontSize: 10)),
        ),
        SizedBox(
          width: 84,
          child: TextButton(
            onPressed: _acting.isNotEmpty
                ? null
                : () => unawaited(_confirm(
                      ignored ? '恢复账号' : '移出号池',
                      ignored
                          ? '从忽略清单恢复该账号并重新扫描入池？'
                          : '移出后该账号不再参与模型调用（凭证保留在 accounts.json，可随时恢复）。',
                      () => _rpc(
                          ignored ? 'pool.restore' : 'pool.logout',
                          <String, dynamic>{'account_id': id},
                          id),
                    )),
            style: TextButton.styleFrom(
              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
              minimumSize: Size.zero,
              tapTargetSize: MaterialTapTargetSize.shrinkWrap,
              backgroundColor: (ignored ? _accent : _dead).withValues(alpha: 0.12),
              shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(6),
                  side: BorderSide(color: (ignored ? _accent : _dead).withValues(alpha: 0.5))),
            ),
            child: Text(ignored ? '恢复' : '退出登录',
                style: TextStyle(
                    color: ignored ? _accent : _dead, fontSize: 11)),
          ),
        ),
      ]),
    );
  }

  // 模型行：两行式——信息+启用开关；思考档 / 图片 / 上下文预算 / 拉起。
  // groupIds 非空表示该行代表一组同名同倍率的路由变体（如 hy3/hy3-b/hy3-c），
  // 开关作用于整组；避免上游把 9 个变体都叫 "Hy3" 时刷出一整屏重复行。
  Widget _modelRow(Map model, {List<String> groupIds = const <String>[]}) {
    final id = (model['id'] ?? '').toString();
    final name = (model['name'] ?? id).toString();
    final multiplier = model['multiplier'];
    final ctx = model['context_window'];
    final isGroup = groupIds.length > 1;
    // ★ 免费只认后端 promo_free（现已严格=真实倍率 0），绝不因"促销"误标免费——
    // 上游把正在扣费的 x0.11/x0.51 也塞进促销名单，标"限时免费"会误导白嫖判断。
    final promoFree = (model['promo_free'] ?? false) == true;
    final promotion = (model['promotion'] ?? '').toString();
    final image = (model['image'] ?? false) == true;
    final reasoning = (model['reasoning'] ?? false) == true;
    final wire = (model['wire'] ?? '').toString();
    final regionSensitive = (model['region_sensitive'] ?? false) == true;
    final pref = _pref(id);
    // 分组行：整组任一变体启用即算启用；开关一次性作用于全组
    final disabledSet = _disabled;
    final disabled = isGroup
        ? groupIds.every((gid) => disabledSet.contains(gid) || _pref(gid)['enabled'] == false)
        : (disabledSet.contains(id) || pref['enabled'] == false);
    final prefImage = pref.containsKey('image') ? pref['image'] == true : image;
    final thinking = (pref['thinking'] ?? '').toString();
    final ctxBudget = pref['ctx_budget'];
    final category = (model['category'] ?? '').toString();
    final tags = <String>[
      if (category.isNotEmpty) category,
      if (multiplier is num) 'x${multiplier.toStringAsFixed(2)}',
      if (multiplier == null && model.containsKey('multiplier')) '倍率未取到',
      if (ctx is num && ctx > 0) '原生 ${(ctx / 1000).toStringAsFixed(0)}K',
      if (ctxBudget is num && ctxBudget > 0)
        '预算 ${(ctxBudget / 1000).toStringAsFixed(0)}K',
      if (image) '多模态',
      if (wire == 'responses') 'responses',
      if (wire == 'messages') 'messages',
      if (regionSensitive) '分区敏感',
    ];
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 5),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
        Row(children: <Widget>[
          Expanded(
            flex: 6,
            child: Row(children: <Widget>[
              Flexible(
                child: Text(isGroup ? name : (model['display'] ?? name).toString(),
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(
                        color: disabled ? _hint : Colors.white, fontSize: 12)),
              ),
              if (promoFree) ...<Widget>[
                const SizedBox(width: 6),
                _pill('免费', _promo),
              ] else if (promotion.isNotEmpty) ...<Widget>[
                // 促销 ≠ 免费：单独灰标，倍率仍在右侧显示真实值
                const SizedBox(width: 6),
                _pill('促销中', _hint),
              ],
            ]),
          ),
          Expanded(
            flex: 4,
            child: Text(tags.join(' · '),
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(color: _hint, fontSize: 10)),
          ),
          SizedBox(
            height: 26,
            child: Switch(
              value: !disabled,
              activeThumbColor: _accent,
              inactiveThumbColor: _hint,
              activeTrackColor: _accent.withValues(alpha: 0.3),
              inactiveTrackColor: _border,
              materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
              onChanged: _acting.isNotEmpty
                  ? null
                  : (value) => unawaited(isGroup
                      ? _toggleModelGroup(groupIds, value)
                      : _toggleModel(id, value)),
            ),
          ),
        ]),
        // 第二行：思考档 / 图片 / 上下文预算 / 拉起
        Padding(
          padding: const EdgeInsets.only(top: 3, left: 2),
          child: Row(children: <Widget>[
            _prefMenu<ThinkingChoice>(
              icon: Icons.psychology_alt_outlined,
              tooltip: '思考档位（reasoning_effort）',
              label: thinking.isEmpty
                  ? '思考:默认'
                  : (thinking == 'off' ? '思考:关' : '思考:$thinking'),
              items: <PopupMenuEntry<ThinkingChoice>>[
                const PopupMenuItem<ThinkingChoice>(
                    value: ThinkingChoice.dflt,
                    child: Text('跟随上游默认',
                        style: TextStyle(color: Colors.white, fontSize: 12))),
                // off 只对「声明可关 + 不在实测硬墙表」的模型提供
                if (reasoning && !_refusesOff.contains(id))
                  const PopupMenuItem<ThinkingChoice>(
                      value: ThinkingChoice.off,
                      child: Text('关闭思考 (off)',
                          style: TextStyle(color: Colors.white, fontSize: 12))),
                for (final level in _thinkingLevels)
                  PopupMenuItem<ThinkingChoice>(
                      value: ThinkingChoice.level(level),
                      child: Text('思考: $level',
                          style: const TextStyle(
                              color: Colors.white, fontSize: 12))),
              ],
              onSelected: (ThinkingChoice choice) => unawaited(_setPref(
                  id, <String, dynamic>{'thinking': choice.wireValue})),
            ),
            const SizedBox(width: 4),
            // 图片开关：上游 supportsImages 为预填，用户可否决
            _iconToggle(
              icon: prefImage ? Icons.image_outlined : Icons.image_not_supported_outlined,
              tooltip: prefImage ? '图片输入：开' : '图片输入：关（已否决）',
              active: prefImage,
              onTap: () => unawaited(_setPref(id, <String, dynamic>{'image': !prefImage})),
            ),
            const SizedBox(width: 4),
            _prefMenu<int>(
              icon: Icons.straighten,
              tooltip: '上下文预算（上限，只能调低不能调高）',
              label: ctxBudget is num && ctxBudget > 0
                  ? 'ctx ${(ctxBudget / 1000).toStringAsFixed(0)}K'
                  : 'ctx 原生',
              items: <PopupMenuEntry<int>>[
                const PopupMenuItem<int>(
                    value: 0,
                    child: Text('原生窗口（清除预算）',
                        style: TextStyle(color: Colors.white, fontSize: 12))),
                for (final k in const <int>[200000, 400000, 1000000])
                  PopupMenuItem<int>(
                      value: k,
                      child: Text('${k ~/ 1000}K',
                          style: const TextStyle(
                              color: Colors.white, fontSize: 12))),
              ],
              onSelected: (int value) => unawaited(
                  _setPref(id, <String, dynamic>{'ctx_budget': value})),
            ),
            const SizedBox(width: 4),
            _iconToggle(
              icon: Icons.play_arrow_rounded,
              tooltip: '拉起测试（单次真实调用）',
              active: true,
              onTap: () => unawaited(_invokeModel(id)),
            ),
          ]),
        ),
      ]),
    );
  }

  Widget _prefMenu<T>({required IconData icon, required String tooltip,
      required String label, required List<PopupMenuEntry<T>> items,
      required void Function(T) onSelected}) =>
      PopupMenuButton<T>(
        tooltip: tooltip,
        onSelected: _acting.isNotEmpty ? null : onSelected,
        itemBuilder: (BuildContext context) => items,
        color: _panel,
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
          decoration: BoxDecoration(
            color: _border.withValues(alpha: 0.5),
            borderRadius: BorderRadius.circular(6),
          ),
          child: Row(mainAxisSize: MainAxisSize.min, children: <Widget>[
            Icon(icon, size: 12, color: _hint),
            const SizedBox(width: 4),
            Text(label, style: const TextStyle(color: _hint, fontSize: 10)),
          ]),
        ),
      );

  Widget _iconToggle({required IconData icon, required String tooltip,
      required bool active, required VoidCallback onTap}) =>
      IconButton(
        tooltip: tooltip,
        onPressed: _acting.isNotEmpty ? null : onTap,
        icon: Icon(icon, size: 15,
            color: active ? _accent : _hint),
        padding: EdgeInsets.zero,
        constraints: const BoxConstraints(minWidth: 26, minHeight: 26),
      );

  Set<String> get _disabled =>
      (( _catalog['disabled_models'] as List<dynamic>? ?? <dynamic>[]))
          .map((e) => e.toString())
          .toSet();

  Map<String, dynamic> _prefs() {
    final raw = _catalog['model_prefs'];
    if (raw is Map) return Map<String, dynamic>.from(raw);
    return <String, dynamic>{};
  }

  Map<String, dynamic> _pref(String modelId) {
    final pref = _prefs()[modelId];
    if (pref is Map) return Map<String, dynamic>.from(pref);
    return <String, dynamic>{};
  }

  Future<void> _setPref(String modelId, Map<String, dynamic> patch) async {
    setState(() => _acting = 'pref-$modelId');
    try {
      final args = <String, dynamic>{'model_id': modelId};
      args.addAll(patch);
      final result = await widget.client.call('pool.model_pref', args);
      if (!mounted) return;
      if (result is Map && result['ok'] == true) {
        final prefs = _prefs();
        prefs[modelId] =
            Map<String, dynamic>.from((result['pref'] as Map?) ?? <String, dynamic>{});
        setState(() => _catalog['model_prefs'] = prefs);
      } else {
        setState(() => _message =
            '设置失败：${result is Map ? (result['error'] ?? '未知原因') : '响应异常'}');
      }
    } catch (error) {
      if (mounted) setState(() => _message = '设置失败：$error');
    } finally {
      if (mounted) setState(() => _acting = '');
    }
  }

  /// 模型拉起：单次真实调用并在弹窗里回显。
  Future<void> _invokeModel(String modelId) async {
    final controller = TextEditingController(text: '你好，用一句话介绍你自己。');
    final ok = await showDialog<bool>(
      context: context,
      builder: (BuildContext context) => AlertDialog(
        backgroundColor: _panel,
        title: Text('拉起 $modelId',
            style: const TextStyle(color: Colors.white, fontSize: 14)),
        content: TextField(
          controller: controller,
          autofocus: true,
          maxLines: 3,
          style: const TextStyle(color: Colors.white, fontSize: 12),
          decoration: const InputDecoration(
              hintText: '测试提示词…',
              hintStyle: TextStyle(color: _hint, fontSize: 12)),
        ),
        actions: <Widget>[
          TextButton(
              onPressed: () => Navigator.of(context).pop(false),
              child: const Text('取消', style: TextStyle(color: _hint))),
          TextButton(
              onPressed: () => Navigator.of(context).pop(true),
              child: const Text('调用', style: TextStyle(color: _accent))),
        ],
      ),
    );
    controller.dispose();
    if (ok != true) return;
    if (!mounted) return;
    setState(() => _acting = 'invoke-$modelId');
    try {
      final result = await widget.client
          .call('pool.invoke', <String, dynamic>{'model': modelId, 'prompt': controller.text});
      if (!mounted) return;
      final text = result is Map && result['ok'] == true
          ? (result['text'] ?? '').toString()
          : '调用失败：${result is Map ? (result['error'] ?? '未知原因') : '响应异常'}';
      await showDialog<void>(
        context: context,
        builder: (BuildContext context) => AlertDialog(
          backgroundColor: _panel,
          title: Text('[$modelId] 回复',
              style: const TextStyle(color: Colors.white, fontSize: 14)),
          content: SingleChildScrollView(
              child: Text(text,
                  style: const TextStyle(color: Colors.white, fontSize: 12, height: 1.5))),
          actions: <Widget>[
            TextButton(
                onPressed: () => Navigator.of(context).pop(),
                child: const Text('关闭', style: TextStyle(color: _accent))),
          ],
        ),
      );
    } finally {
      if (mounted) setState(() => _acting = '');
    }
  }

  Widget _catalogCard() {
    final wb = (_catalog['wb'] as Map<dynamic, dynamic>? ?? <dynamic, dynamic>{});
    final wbModels = (wb['models'] as List<dynamic>? ?? <dynamic>[])
        .whereType<Map>()
        .map(Map<String, dynamic>.from)
        .toList()
      ..sort((a, b) {
        // 档位（快速/均衡/极致）置顶，与客户端下拉一致；再按免费、名称。
        final ta = (a['tier'] ?? false) == true ? 0 : 1;
        final tb = (b['tier'] ?? false) == true ? 0 : 1;
        if (ta != tb) return ta - tb;
        final pa = (a['promo_free'] ?? false) == true ? 0 : 1;
        final pb = (b['promo_free'] ?? false) == true ? 0 : 1;
        if (pa != pb) return pa - pb;
        return (a['name'] ?? '').toString().compareTo((b['name'] ?? '').toString());
      });
    // 已禁用一律隐藏：关掉就消失，不再占据列表
    final disabledSet = _disabled;
    final visibleWb =
        wbModels.where((m) => !disabledSet.contains((m['id'] ?? '').toString())).toList();
    // ★ 同名同倍率合并为一行（2026-11-12 修"9 个 Hy3 刷屏"）：
    //   上游把 hy3/hy3-b/hy3-c/hy4flash-exp1-4 等不同路由变体都命名 "Hy3"，
    //   逐行渲染就是一整屏重复。按 (name, multiplier) 分组，每组取首个为代表，
    //   变体 id 收进 groupIds，行内标"N 个变体"，开关整组生效。
    //   倍率不同的同名模型（如 hy3 x0.00 vs hy3-x x0.05）保持分行——价格不同必须区分。
    final Map<String, List<Map<String, dynamic>>> groups = <String, List<Map<String, dynamic>>>{};
    final List<String> groupOrder = <String>[];
    for (final m in visibleWb) {
      final n = (m['name'] ?? m['id'] ?? '').toString();
      final mult = m['multiplier'];
      final key = '$n|${mult is num ? mult.toStringAsFixed(2) : 'none'}';
      if (!groups.containsKey(key)) {
        groups[key] = <Map<String, dynamic>>[];
        groupOrder.add(key);
      }
      groups[key]!.add(m);
    }
    // 沿用上面的排序：档位组优先 → 免费组 → name
    groupOrder.sort((a, b) {
      final ga = groups[a]!.first;
      final gb = groups[b]!.first;
      final ta = (ga['tier'] ?? false) == true ? 0 : 1;
      final tb = (gb['tier'] ?? false) == true ? 0 : 1;
      if (ta != tb) return ta - tb;
      final pa = (ga['promo_free'] ?? false) == true ? 0 : 1;
      final pb = (gb['promo_free'] ?? false) == true ? 0 : 1;
      if (pa != pb) return pa - pb;
      return (ga['name'] ?? '').toString().compareTo((gb['name'] ?? '').toString());
    });
    final groupRows = groupOrder.map((key) => MapEntry(
        groups[key]!.first,
        groups[key]!.map((m) => (m['id'] ?? '').toString()).where((s) => s.isNotEmpty).toList(),
    )).toList();
    // ★ 供应商免费模型并入目录（2026-11-12）：硅基流动/OpenRouter 的模型也在此
    //   显示、可单独启用/禁用。用 display 前缀标来源，复用 _modelRow 的 _pref 开关。
    //   只展示有模型清单的供应商；custom_openai(0 模型) 跳过。
    final List<Widget> supplierBlocks = <Widget>[];
    supplierBlocks.add(const Padding(
      padding: EdgeInsets.only(top: 8, bottom: 2),
      child: Text('云供应商免费模型（来自注册表；开关仅影响记忆偏好，'
          '实际调用的模型在下方「云供应商」卡片的下拉里选）',
          style: TextStyle(color: _hint, fontSize: 10, height: 1.4)),
    ));
    for (final reg in _registry) {
      final rid = (reg['id'] ?? '').toString();
      final rname = (reg['name'] ?? rid).toString();
      final models = (reg['models'] as List<dynamic>? ?? <dynamic>[])
          .whereType<Map>()
          .map(Map<String, dynamic>.from)
          .toList();
      if (models.isEmpty) continue;
      supplierBlocks.add(Padding(
        padding: const EdgeInsets.only(top: 10),
        child: Row(children: <Widget>[
          Icon(_supplierIcon(rid), size: 13, color: _accent),
          const SizedBox(width: 5),
          Text(rname,
              style: const TextStyle(color: _accent, fontSize: 11,
                  fontWeight: FontWeight.w600)),
        ]),
      ));
      for (final m in models) {
        final mid = (m['id'] ?? '').toString();
        final cat = (m['category'] ?? '').toString();
        final ctx = m['contextWindow'];
        supplierBlocks.add(_modelRow(<String, dynamic>{
          'id': mid,
          'display': '$rname · ${m['name'] ?? mid}',
          'multiplier': 0.0,
          'promo_free': true,
          'context_window': ctx is num ? ctx : null,
          'image': (cat == 'llm-vision'),
          'reasoning': (m['reasoning'] == true),
          'category': cat,
        }));
      }
    }
    final live = wb['live'] == true;
    return Container(
      margin: const EdgeInsets.fromLTRB(16, 10, 16, 0),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: _panel,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: _border),
      ),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
        Row(children: <Widget>[
          const Text('模型目录',
              style: TextStyle(color: Colors.white, fontSize: 14,
                  fontWeight: FontWeight.w600)),
          const SizedBox(width: 8),
          _pill(live ? '上游实时' : '静态兜底', live ? _ok : _hint),
          const Spacer(),
          // 「同步模型表」：强制重拉 WorkBuddy /v3/config
          _actionButton('同步模型表', Icons.sync, 'catalog',
              () => unawaited(_rpc('pool.catalog',
                  <String, dynamic>{'force': true}, 'catalog',
                  okMessage: '模型目录已刷新'))),
        ]),
        const SizedBox(height: 6),
        const Row(children: <Widget>[
          Expanded(flex: 4, child: Text('模型',
              style: TextStyle(color: _hint, fontSize: 10))),
          Expanded(flex: 4, child: Text('倍率 · 上下文 · 能力',
              style: TextStyle(color: _hint, fontSize: 10))),
          SizedBox(width: 40, child: Text('启用',
              style: TextStyle(color: _hint, fontSize: 10))),
        ]),
        const Divider(color: _border, height: 12),
        if (wbModels.isEmpty)
          const Padding(
            padding: EdgeInsets.symmetric(vertical: 10),
            child: Text('WorkBuddy 目录未取到（账号未入池或上游不可达）。',
                style: TextStyle(color: _hint, fontSize: 12)),
          )
        else ...groupRows.map((entry) => _modelRow(entry.key, groupIds: entry.value)),
        if (supplierBlocks.isNotEmpty) ...<Widget>[
          const Divider(color: _border, height: 14),
          ...supplierBlocks,
        ],
      ]),
    );
  }

  IconData _supplierIcon(String id) {
    switch (id) {
      case 'siliconflow': return Icons.bolt;
      case 'open_router': return Icons.router;
      case 'volcengine-ark':
      case 'byteplus': return Icons.cloud;
      default: return Icons.extension;
    }
  }

  /// 类别中文标签（防止把向量/精排/TTS 当对话模型选错）。
  String _catLabel(String cat) {
    switch (cat) {
      case 'embedding': return '向量';
      case 'rerank': return '精排';
      case 'llm': return '对话';
      case 'llm-code': return '代码';
      case 'llm-vision': return '视觉';
      case 'asr': return '语音识别';
      case 'asr-diarization': return '声纹分离';
      case 'translation': return '翻译';
      case 'tts': return '语音合成';
      case 'router': return '路由';
      default: return cat.isEmpty ? '未标注' : cat;
    }
  }

  /// 只有这几类走 OpenAI /chat/completions，能当"对话模型"填进路由。
  /// embedding/rerank/tts/asr 是专用端点，选了会 404/400，禁止当作对话模型保存。
  bool _isChatModel(String cat) => cat == 'llm' || cat == 'llm-code' ||
      cat == 'llm-vision' || cat == 'router';

  // ── 浏览器登录 · 添加账号：云供应商 API key ────────────────────────────
  // 每行：名称（N 模型）+ 模型下拉 + API key 输入 + 保存（存系统凭据库并热重载）
  final Map<String, String> _registryModelPick = <String, String>{};
  final Map<String, TextEditingController> _registryKeyControllers =
      <String, TextEditingController>{};

  Widget _registryCard() {
    if (_registry.isEmpty) return const SizedBox.shrink();
    return Container(
      margin: const EdgeInsets.fromLTRB(16, 10, 16, 0),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: _panel, borderRadius: BorderRadius.circular(10),
        border: Border.all(color: _border)),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
        const Text('浏览器登录 · 添加账号（云供应商 API key）',
            style: TextStyle(color: Colors.white, fontSize: 14,
                fontWeight: FontWeight.w600)),
        const SizedBox(height: 4),
        const Text(
            'WorkBuddy 新账号：点上方「浏览器登录」扫码确认即自动入池（OAuth 凭证落共享 vault）；'
            '或点「拉起桌面端」手动切号后重扫。'
            '下面云供应商只保留硅基流动 / OpenRouter / 字节 / 自定义：填 key 保存即热重载进聊天路由。',
            style: TextStyle(color: _hint, fontSize: 11)),
        const SizedBox(height: 6),
        ExpansionTile(
          tilePadding: EdgeInsets.zero,
          childrenPadding: EdgeInsets.zero,
          iconColor: _hint, collapsedIconColor: _hint,
          title: Text('云供应商',
              style: const TextStyle(color: Colors.white, fontSize: 13,
                  fontWeight: FontWeight.w600)),
          children: <Widget>[
            for (final reg in _registry) _registryRow(reg),
          ],
        ),
      ]),
    );
  }

  Widget _registryRow(Map reg) {
    final id = (reg['id'] ?? '').toString();
    final name = (reg['name'] ?? id).toString();
    final baseUrl = (reg['base_url'] ?? '').toString();
    final models = (reg['models'] as List<dynamic>? ?? <dynamic>[])
        .whereType<Map>()
        .map(Map<String, dynamic>.from)
        .toList();
    // 默认选第一个「可对话」模型（向量/精排/TTS 不能填进 chat 路由）
    final chatModels = models.where((m) => _isChatModel((m['category'] ?? '').toString())).toList();
    Map<String, dynamic>? defaultModel = chatModels.isNotEmpty
        ? chatModels.first : (models.isNotEmpty ? models.first : null);
    final picked = _registryModelPick[id] ?? defaultModel?['id']?.toString() ?? '';
    // 当前选中模型的类别 → 非对话模型禁止保存
    final pickedCat = (models.cast<Map<String, dynamic>?>().firstWhere(
        (m) => (m?['id'] ?? '').toString() == picked, orElse: () => null)?['category'] ?? '').toString();
    final pickedIsChat = _isChatModel(pickedCat);
    final controller = _registryKeyControllers.putIfAbsent(
        id, () => TextEditingController());
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 5),
      child: Row(children: <Widget>[
        Expanded(
          flex: 4,
          child: Column(crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
            Text(name, style: const TextStyle(color: _accent, fontSize: 12)),
            Text(baseUrl,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(color: _hint, fontSize: 9)),
          ]),
        ),
        const SizedBox(width: 8),
        // 模型下拉
        SizedBox(
          width: 150,
          child: DropdownButtonFormField<String>(
            initialValue: picked.isEmpty ? null : picked,
            isDense: true,
            dropdownColor: _panel,
            style: const TextStyle(color: Colors.white, fontSize: 11),
            decoration: InputDecoration(
              isDense: true, contentPadding: const EdgeInsets.symmetric(
                  horizontal: 8, vertical: 6),
              enabledBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(6),
                  borderSide: const BorderSide(color: _border)),
            ),
            items: <DropdownMenuItem<String>>[
              for (final m in models) ...<DropdownMenuItem<String>>[
                DropdownMenuItem<String>(
                    value: (m['id'] ?? '').toString(),
                    enabled: _isChatModel((m['category'] ?? '').toString()),
                    child: Text(
                        '[${_catLabel((m['category'] ?? '').toString())}] ${m['name'] ?? m['id']}',
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(
                            color: _isChatModel((m['category'] ?? '').toString())
                                ? Colors.white : Colors.grey,
                            fontSize: 10))),
              ],
            ],
            onChanged: (value) =>
                setState(() => _registryModelPick[id] = value ?? picked),
          ),
        ),
        const SizedBox(width: 8),
        Expanded(
          flex: 3,
          child: TextField(
            controller: controller,
            obscureText: true,
            style: const TextStyle(color: Colors.white, fontSize: 11),
            decoration: InputDecoration(
              hintText: '粘贴 API key', hintStyle: const TextStyle(
                  color: _hint, fontSize: 10),
              isDense: true, contentPadding: const EdgeInsets.symmetric(
                  horizontal: 8, vertical: 8),
              filled: true, fillColor: _bg,
              enabledBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(6),
                  borderSide: const BorderSide(color: _border)),
            ),
          ),
        ),
        const SizedBox(width: 8),
        Tooltip(
          message: pickedIsChat
              ? '保存后自动开启云路由，聊天/抽取/巩固可选 provider:$id'
              : '「${_catLabel(pickedCat)}」不是对话模型，走专用端点，不能填进 chat 路由；请选 [对话]/[视觉]/[代码]/[路由]',
          child: TextButton(
            onPressed: _acting.isNotEmpty || picked.isEmpty || controller.text.trim().isEmpty || !pickedIsChat
                ? null
                : () => unawaited(_saveRegistryProvider(
                    id: id, baseUrl: baseUrl, model: picked,
                    apiKey: controller.text.trim())),
            style: TextButton.styleFrom(
              backgroundColor: _accent.withValues(alpha: 0.14),
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
              shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(6)),
            ),
            child: Text(pickedIsChat ? '保存' : '非对话',
                style: TextStyle(color: pickedIsChat ? _accent : Colors.grey, fontSize: 12)),
          ),
        ),
      ]),
    );
  }

  Future<void> _saveRegistryProvider({required String id, required String baseUrl,
      required String model, required String apiKey}) async {
    setState(() => _acting = 'reg-$id');
    try {
      final result = await widget.client.call('router.provider_set', <String, dynamic>{
        'name': id, 'endpoint': baseUrl, 'kind': 'openai',
        'model': model, 'api_key': apiKey,
      });
      if (!mounted) return;
      setState(() => _message = (result is Map && result['ok'] == true)
          ? '$id 已保存并热重载（聊天路由可用 provider:$id）'
          : '保存失败：${result is Map ? (result['error'] ?? '') : ''}');
    } catch (error) {
      if (mounted) setState(() => _message = '保存失败：$error');
    } finally {
      if (mounted) setState(() => _acting = '');
    }
  }

  @override
  Widget build(BuildContext context) {
    final enabled = (_data['enabled'] ?? false) == true;
    final defaultModel = (_data['default_model'] ?? '').toString();
    final accountCount = (_data['account_count'] ?? 0) as int;
    final ignoredCount = (_data['ignored_count'] ?? 0) as int;
    final inRouter = (_data['in_router'] ?? false) == true;
    final accounts = (_data['accounts'] as List<dynamic>? ?? <dynamic>[])
        .whereType<Map>()
        .map(Map<String, dynamic>.from)
        .toList()
      ..sort((a, b) {
        final ia = (a['ignored'] ?? false) == true ? 1 : 0;
        final ib = (b['ignored'] ?? false) == true ? 1 : 0;
        if (ia != ib) return ia - ib;
        return ((a['label'] ?? '').toString())
            .compareTo((b['label'] ?? '').toString());
      });
    return Scaffold(
      backgroundColor: _bg,
      body: RefreshIndicator(
        onRefresh: () => _load(),
        child: ListView(padding: const EdgeInsets.only(bottom: 16), children: <Widget>[
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 14, 16, 4),
            child: Row(children: <Widget>[
              const Text('号池网关',
                  style: TextStyle(color: Colors.white, fontSize: 18)),
              const SizedBox(width: 10),
              _pill(enabled ? '号池启用' : '号池未启用', enabled ? _ok : _hint),
              const Spacer(),
              IconButton(
                tooltip: '刷新',
                onPressed: _busy ? null : () => unawaited(_load()),
                icon: const Icon(Icons.refresh, color: _hint, size: 20),
              ),
            ]),
          ),
          if (_message.isNotEmpty)
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 0, 16, 4),
              child: Text(_message,
                  style: const TextStyle(color: _accent, fontSize: 11)),
            ),
          if (!inRouter)
            const Padding(
              padding: EdgeInsets.fromLTRB(16, 4, 16, 4),
              child: Text(
                  '号池未装配进模型路由（.env 设 POOL_ENABLED=true 后重启后端）。',
                  style: TextStyle(color: _warn, fontSize: 12)),
            ),
          // ── 账号池卡片 ────────────────────────────────────────────────
          Container(
            margin: const EdgeInsets.fromLTRB(16, 8, 16, 0),
            padding: const EdgeInsets.all(14),
            decoration: BoxDecoration(
              color: _panel,
              borderRadius: BorderRadius.circular(10),
              border: Border.all(color: _border),
            ),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start,
                children: <Widget>[
              Row(children: <Widget>[
                const Text('账号池',
                    style: TextStyle(color: Colors.white, fontSize: 14,
                        fontWeight: FontWeight.w600)),
                const SizedBox(width: 8),
                Text('在池 $accountCount · 已移出 $ignoredCount · 默认模型 $defaultModel',
                    style: const TextStyle(color: _hint, fontSize: 11)),
                const Spacer(),
                _actionButton('重新扫描凭证', Icons.radar, 'rescan',
                    () => unawaited(_rpc('pool.rescan', <String, dynamic>{}, 'rescan',
                        okMessage: '重新扫描完成'))),
                const SizedBox(width: 6),
                // 原版「一键签到」：全部活跃账号领每日奖励
                _actionButton('一键签到', Icons.verified, 'checkin',
                    () => unawaited(_rpc('pool.checkin', <String, dynamic>{}, 'checkin',
                        okMessage: '签到完成'))),
                const SizedBox(width: 6),
                // ★ 浏览器扫码登录（OAuth 闭环）：复用 wbmod 号池网关，扫码后凭证落共享
                //   vault，Python 号池 discover 已扫该目录 → 自动入池，无需手动重扫。
                _actionButton('浏览器登录', Icons.qr_code, 'oauth',
                    () => unawaited(_oauthLogin()), tone: _ok),
                const SizedBox(width: 6),
                // 拉起 WorkBuddy 桌面端（备用路径）：桌面端一次只登一个账号，
                // 手动切号后点「重新扫描凭证」抓进池。
                _actionButton('拉起桌面端', Icons.login, 'login',
                    () => unawaited(_rpc('pool.login_start', <String, dynamic>{}, 'login',
                        okMessage: '已拉起桌面端；请手动切到要加的账号，再点「重新扫描凭证」'))),
              ]),
              const SizedBox(height: 6),
              const Row(children: <Widget>[
                Expanded(flex: 4, child: Text('账号',
                    style: TextStyle(color: _hint, fontSize: 10))),
                Expanded(flex: 2, child: Text('区域',
                    style: TextStyle(color: _hint, fontSize: 10))),
                Expanded(flex: 4, child: Text('状态',
                    style: TextStyle(color: _hint, fontSize: 10))),
                Expanded(flex: 2, child: Text('已服务',
                    style: TextStyle(color: _hint, fontSize: 10))),
                SizedBox(width: 84, child: Text('操作',
                    style: TextStyle(color: _hint, fontSize: 10))),
              ]),
              const Divider(color: _border, height: 12),
              if (accounts.isEmpty)
                const Padding(
                  padding: EdgeInsets.symmetric(vertical: 14),
                  child: Text(
                      '加账号：①点「浏览器登录」→ 扫码确认 → 凭证自动入池（推荐，可多账号并存）；'
                      '或点「拉起桌面端」→ 在客户端手动切到要加的账号 → 回本页点「重新扫描凭证」。'
                      '两条路径的凭证共享，已在 wbmod 号池登录的账号重扫后也能看到。',
                      style: TextStyle(color: _hint, fontSize: 12)),
                )
              else ...accounts.map(_accountRow),
            ]),
          ),
          _catalogCard(),
          // ── 浏览器登录 · 添加账号（77 家云供应商，xdpool 原版布局）────────
          _registryCard(),
          // ── 能力说明 ──────────────────────────────────────────────────
          Container(
            margin: const EdgeInsets.fromLTRB(16, 10, 16, 0),
            padding: const EdgeInsets.all(14),
            decoration: BoxDecoration(
              color: _panel.withValues(alpha: 0.6),
              borderRadius: BorderRadius.circular(10),
              border: Border.all(color: _border.withValues(alpha: 0.6)),
            ),
            child: const Column(crossAxisAlignment: CrossAxisAlignment.start,
                children: <Widget>[
              Text('说明',
                  style: TextStyle(color: Colors.white, fontSize: 13,
                      fontWeight: FontWeight.w600)),
              SizedBox(height: 6),
              Text(
                  '· 倍率/限时免费来自上游实时目录，绝不硬编码（价格是"现在的事实"）；'
                  '静态兜底只保证有 roster，不标价格。\n'
                  '· 「一键签到」走 billing 端点逐账号领取，已签到/无活动按幂等成功处理。\n'
                  '· 加密登录态（\$wbEncrypted）经 WorkBuddy 桌面端原生密钥解密，'
                  '密钥不落盘、不日志。\n'
                  '· 「退出登录」＝移出号池：凭证保留在 accounts.json，随时可恢复。',
                  style: TextStyle(color: _hint, fontSize: 11, height: 1.6)),
            ]),
          ),
        ]),
      ),
    );
  }

  Future<void> _confirm(String title, String content, Future<void> Function() act) async {
    final yes = await showDialog<bool>(
      context: context,
      builder: (BuildContext context) => AlertDialog(
        backgroundColor: _panel,
        title: Text(title, style: const TextStyle(color: Colors.white, fontSize: 15)),
        content: Text(content, style: const TextStyle(color: _hint, fontSize: 12)),
        actions: <Widget>[
          TextButton(
              onPressed: () => Navigator.of(context).pop(false),
              child: const Text('取消', style: TextStyle(color: _hint))),
          TextButton(
              onPressed: () => Navigator.of(context).pop(true),
              child: const Text('确定', style: TextStyle(color: _accent))),
        ],
      ),
    );
    if (yes == true) await act();
  }
}

/// 思考档位选择项（"" = 跟随上游默认）。
class ThinkingChoice {
  const ThinkingChoice(this.wireValue);

  final String wireValue;

  static const ThinkingChoice dflt = ThinkingChoice('');
  static const ThinkingChoice off = ThinkingChoice('off');
  static ThinkingChoice level(String level) => ThinkingChoice(level);
}
