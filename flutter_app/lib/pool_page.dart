import 'dart:async';

import 'package:flutter/material.dart';

import 'brain_client.dart';

/// 号池网关 —— 对齐 dsh-xdpool 原版：账号池表格（账号/区域/状态/已服务/操作）+
/// 重新扫描凭证 + 一键签到 + 模型目录（WorkBuddy 倍率/上下文/限时免费/开关）+
/// 免密 lane 全量免费模型（70+，opencode listing 实时）+ 拉起 WorkBuddy 登录。
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
  bool _showDisabled = false;
  String _message = '';
  bool _busy = false;
  String _acting = '';

  @override
  void initState() {
    super.initState();
    unawaited(_load());
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
                ? _pill('冷却(${cooldowns.length}模型)', _warn)
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
  Widget _modelRow(Map model) {
    final id = (model['id'] ?? '').toString();
    final name = (model['name'] ?? id).toString();
    final multiplier = model['multiplier'];
    final ctx = model['context_window'];
    final promoFree = (model['promo_free'] ?? false) == true;
    final image = (model['image'] ?? false) == true;
    final reasoning = (model['reasoning'] ?? false) == true;
    final wire = (model['wire'] ?? '').toString();
    final regionSensitive = (model['region_sensitive'] ?? false) == true;
    final pref = _pref(id);
    final disabled = _disabled.contains(id) || pref['enabled'] == false;
    final prefImage = pref.containsKey('image') ? pref['image'] == true : image;
    final thinking = (pref['thinking'] ?? '').toString();
    final ctxBudget = pref['ctx_budget'];
    final tags = <String>[
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
                child: Text((model['display'] ?? name).toString(),
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(
                        color: disabled ? _hint : Colors.white, fontSize: 12)),
              ),
              if (promoFree) ...<Widget>[
                const SizedBox(width: 6),
                _pill('限时免费', _promo),
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
                  : (value) => unawaited(_toggleModel(id, value)),
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
    final free = (_catalog['free'] as Map<dynamic, dynamic>? ?? <dynamic, dynamic>{});
    final wbModels = (wb['models'] as List<dynamic>? ?? <dynamic>[])
        .whereType<Map>()
        .map(Map<String, dynamic>.from)
        .toList()
      ..sort((a, b) {
        final pa = (a['promo_free'] ?? false) == true ? 0 : 1;
        final pb = (b['promo_free'] ?? false) == true ? 0 : 1;
        if (pa != pb) return pa - pb;
        return (a['name'] ?? '').toString().compareTo((b['name'] ?? '').toString());
      });
    // 禁用的不显示（原版行为）；需要恢复时点「显示已禁用」
    final disabledSet = _disabled;
    final visibleWb = _showDisabled
        ? wbModels
        : wbModels.where((m) => !disabledSet.contains((m['id'] ?? '').toString())).toList();
    // 同名变体（hy3/hy3-b/hy3-c… 上游名字都叫 Hy3）：重名时后缀 id 区分
    final nameCounts = <String, int>{};
    for (final m in visibleWb) {
      final n = (m['name'] ?? '').toString();
      nameCounts[n] = (nameCounts[n] ?? 0) + 1;
    }
    for (final m in visibleWb) {
      final n = (m['name'] ?? '').toString();
      final id = (m['id'] ?? '').toString();
      if ((nameCounts[n] ?? 0) > 1 && id != n) {
        m['display'] = '$n · $id';
      } else {
        m['display'] = n;
      }
    }
    final freeModels = (free['models'] as List<dynamic>? ?? <dynamic>[])
        .whereType<Map>()
        .map(Map<String, dynamic>.from)
        .toList()
      ..sort((a, b) => (a['name'] ?? '').toString().compareTo((b['name'] ?? '').toString()));
    final freeLaneOn = (_data['free_lane'] ?? false) == true;
    final live = (wb['live'] == true) || (free['live'] == true);
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
          const SizedBox(width: 6),
          // 原版行为：禁用即从列表消失；这里留恢复口
          InkWell(
            onTap: () => setState(() => _showDisabled = !_showDisabled),
            child: Text(
                _showDisabled ? '隐藏已禁用' : '显示已禁用（${disabledSet.length}）',
                style: const TextStyle(color: _hint, fontSize: 10)),
          ),
          const Spacer(),
          // 原版「同步模型表」：强制重拉 /v3/config + 免密 listing
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
        else ...visibleWb.map(_modelRow),
        const Divider(color: _border, height: 14),
        // 免密 lane：70+ 免费模型（实时 listing，静态兜底保底）
        ExpansionTile(
          tilePadding: EdgeInsets.zero,
          childrenPadding: EdgeInsets.zero,
          iconColor: _hint,
          collapsedIconColor: _hint,
          title: Row(children: <Widget>[
            Text('免密免费模型 · ${freeModels.length} 个',
                style: const TextStyle(color: Colors.white, fontSize: 13,
                    fontWeight: FontWeight.w600)),
            const SizedBox(width: 8),
            _pill(freeLaneOn ? 'lane 开' : 'lane 关（配置 free_lane）',
                freeLaneOn ? _ok : _warn),
            if ((free['live'] ?? false) == true) ...<Widget>[
              const SizedBox(width: 6),
              _pill('实时', _ok),
            ],
          ]),
          subtitle: const Text(
              'opencode.ai 免密通道（Bearer public），无需账号；倍率恒为 0，'
              '分区敏感模型需出口网络匹配。',
              style: TextStyle(color: _hint, fontSize: 10)),
          children: <Widget>[
            if (freeModels.isEmpty)
              const Padding(
                padding: EdgeInsets.symmetric(vertical: 8),
                child: Text('免费模型清单未取到。',
                    style: TextStyle(color: _hint, fontSize: 12)),
              )
            else ...freeModels.map(_modelRow),
          ],
        ),
      ]),
    );
  }

  // ── 浏览器登录 · 添加账号：77 家 API 供应商（xdpool 原版布局移植）──────
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
            'WorkBuddy 新账号点右上「多账号 → 浏览器登录」扫码确认，凭证自动入池（多账号并存）；'
            '下面 77 家是 API key 供应商：填 key 保存即热重载进聊天路由。',
            style: TextStyle(color: _hint, fontSize: 11)),
        const SizedBox(height: 6),
        ExpansionTile(
          tilePadding: EdgeInsets.zero,
          childrenPadding: EdgeInsets.zero,
          iconColor: _hint, collapsedIconColor: _hint,
          title: Text('云供应商 · ${_registry.length} 家',
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
    final picked = _registryModelPick[id] ??
        (models.isNotEmpty ? (models.first['id'] ?? '').toString() : '');
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
            Text('$baseUrl · ${reg['model_count']} 模型',
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
              for (final m in models)
                DropdownMenuItem<String>(
                    value: (m['id'] ?? '').toString(),
                    child: Text((m['name'] ?? '').toString(),
                        overflow: TextOverflow.ellipsis)),
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
        TextButton(
          onPressed: _acting.isNotEmpty || picked.isEmpty || controller.text.trim().isEmpty
              ? null
              : () => unawaited(_saveRegistryProvider(
                  id: id, baseUrl: baseUrl, model: picked,
                  apiKey: controller.text.trim())),
          style: TextButton.styleFrom(
            backgroundColor: _accent.withValues(alpha: 0.14),
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
            shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(6)),
          ),
          child: const Text('保存', style: TextStyle(color: _accent, fontSize: 12)),
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
    final freeLane = (_data['free_lane'] ?? false) == true;
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
              const SizedBox(width: 6),
              _pill(freeLane ? '免密 lane 开' : '免密 lane 关', freeLane ? _ok : _hint),
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
                // 浏览器登录：直接跳系统默认浏览器登录页；登录后换号会被
                // 凭证监听自动快照入池（多账号并存）
                _actionButton('拉起登录', Icons.open_in_new, 'login',
                    () => unawaited(_rpc('pool.login_url',
                        <String, dynamic>{'url': 'https://www.workbuddy.cn/'}, 'login',
                        okMessage: '已在浏览器打开登录页；登录后自动入池（多账号并存）'))),
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
                      '账号池为空。点「拉起登录」打开 WorkBuddy 桌面端登录，'
                      '登录态（含加密态自动解密）落盘后点「重新扫描凭证」自动入池；'
                      '已有 xdpool accounts.json 的机器重扫即入。',
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
                  '· 「退出登录」＝移出号池：凭证保留在 accounts.json，随时可恢复。\n'
                  '· 免密 lane 是共享第三方端点，有封号风险（默认关，本次按你的要求开启）。',
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
