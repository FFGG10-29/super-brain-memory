import 'dart:async';

import 'package:flutter/material.dart';

import 'brain_client.dart';

/// 号池页 —— 对齐 dsh-xdpool 原版布局：账号池表格（账号/区域/状态/操作）+
/// 重新扫描凭证。操作全部是后端真实能力：
///   pool.status（状态）/ pool.rescan（重新扫描凭证）
///   pool.logout（移出号池，凭证保留可恢复）/ pool.restore（恢复入池）
/// 模型目录倍率、一键签到、浏览器登录是外部 xdpool 面板能力，未内置（见页脚说明）。
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

  Map<String, dynamic> _data = <String, dynamic>{};
  String _message = '';
  bool _busy = false;
  String _acting = '';

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  Future<void> _load() async {
    setState(() => _busy = true);
    try {
      final data = await widget.client.poolStatus();
      if (!mounted) return;
      setState(() {
        _data = data;
        _message = '';
      });
    } catch (error) {
      if (mounted) setState(() => _message = '加载失败：$error');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _rpc(String method, Map<String, dynamic> args, String tag) async {
    if (_acting.isNotEmpty) return;
    setState(() => _acting = tag);
    try {
      final result = await widget.client.call(method, args);
      if (!mounted) return;
      if (result is Map && result['ok'] == true) {
        await _load();
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

  Widget _pill(String text, Color tone) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
        decoration: BoxDecoration(
          color: tone.withValues(alpha: 0.14),
          borderRadius: BorderRadius.circular(999),
          border: Border.all(color: tone.withValues(alpha: 0.5)),
        ),
        child: Text(text, style: TextStyle(color: tone, fontSize: 10)),
      );

  // 账号行：● 账号 | 区域 | 状态 | 操作（移出号池/恢复）——对齐原版表格列。
  Widget _accountRow(Map account) {
    final id = (account['id'] ?? '').toString();
    final label = (account['label'] ?? id).toString();
    final region = (account['region'] ?? '').toString();
    final ignored = (account['ignored'] ?? false) == true;
    final dead = (account['dead'] ?? false) == true;
    final deadSec = (account['dead_remaining_sec'] ?? 0) as int;
    final cooldowns = (account['model_cooldowns'] as Map<dynamic, dynamic>? ?? {});
    final status = ignored
        ? _pill('已移出', _hint)
        : dead
            ? _pill('登录态失效 ${deadSec}s', _dead)
            : (cooldowns.isNotEmpty
                ? _pill('冷却中(${cooldowns.length}模型)', _warn)
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
        onRefresh: _load,
        child: ListView(padding: const EdgeInsets.only(bottom: 16), children: <Widget>[
          // 顶栏：标题 + 状态 pills + 刷新
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
                onPressed: _busy ? null : _load,
                icon: const Icon(Icons.refresh, color: _hint, size: 20),
              ),
            ]),
          ),
          if (_message.isNotEmpty)
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 0, 16, 4),
              child: Text(_message,
                  style: const TextStyle(color: _dead, fontSize: 11)),
            ),
          if (!inRouter)
            const Padding(
              padding: EdgeInsets.fromLTRB(16, 4, 16, 4),
              child: Text('号池未装配进模型路由（config.account_pool.enabled）——先在配置中启用。',
                  style: TextStyle(color: _warn, fontSize: 12)),
            ),
          // ── 账号池卡片（原版表格布局）────────────────────────────────
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
                // 原版「重新扫描凭证」
                TextButton.icon(
                  onPressed: _acting.isNotEmpty ? null : () =>
                      unawaited(_rpc('pool.rescan', <String, dynamic>{}, 'rescan')),
                  icon: _acting == 'rescan'
                      ? const SizedBox(width: 12, height: 12,
                          child: CircularProgressIndicator(strokeWidth: 2, color: _accent))
                      : const Icon(Icons.radar, size: 14, color: _accent),
                  label: const Text('重新扫描凭证',
                      style: TextStyle(color: _accent, fontSize: 12)),
                  style: TextButton.styleFrom(
                    backgroundColor: _accent.withValues(alpha: 0.1),
                    padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
                    shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(6),
                        side: BorderSide(color: _accent.withValues(alpha: 0.4))),
                  ),
                ),
              ]),
              const SizedBox(height: 6),
              // 表头（对齐原版：账号/区域/状态/操作）
              const Padding(
                padding: EdgeInsets.only(left: 16),
                child: Row(children: <Widget>[
                  Expanded(flex: 4, child: Text('账号',
                      style: TextStyle(color: _hint, fontSize: 10))),
                  Expanded(flex: 2, child: Text('区域',
                      style: TextStyle(color: _hint, fontSize: 10))),
                  Expanded(flex: 4, child: Text('状态',
                      style: TextStyle(color: _hint, fontSize: 10))),
                  SizedBox(width: 84, child: Text('操作',
                      style: TextStyle(color: _hint, fontSize: 10))),
                ]),
              ),
              const Divider(color: _border, height: 12),
              if (accounts.isEmpty)
                const Padding(
                  padding: EdgeInsets.symmetric(vertical: 14),
                  child: Text(
                      '账号池为空。凭证来源：xdpool accounts.json 与 WorkBuddy 登录态'
                      '（自动发现）；点「重新扫描凭证」重扫。',
                      style: TextStyle(color: _hint, fontSize: 12)),
                )
              else
                ...accounts.map(_accountRow),
            ]),
          ),
          // ── 能力边界说明（诚实：哪些是外部 xdpool 能力）────────────────
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
              Text('与原版 xdpool 面板的差异',
                  style: TextStyle(color: Colors.white, fontSize: 13,
                      fontWeight: FontWeight.w600)),
              SizedBox(height: 6),
              Text(
                  '· 模型目录（倍率/存ctx/禁用/限时免费）与「当前 agents 模型」是外部 '
                  'dsh-xdpool 面板概念，超级大脑后端无对应数据结构，未做假界面。\n'
                  '· 「一键签到」「浏览器登录」依赖 xdpool 的桌面自动化，未内置；'
                  '新账号走 WorkBuddy 桌面端登录后，点「重新扫描凭证」自动入池。\n'
                  '· 「退出登录」＝移出号池：凭证保留在 accounts.json，随时可恢复。',
                  style: TextStyle(color: _hint, fontSize: 11, height: 1.6)),
            ]),
          ),
        ]),
      ),
    );
  }
}
