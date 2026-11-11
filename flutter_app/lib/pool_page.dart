import 'dart:async';

import 'package:flutter/material.dart';

import 'brain_client.dart';

/// 号池页 —— 信息架构与视觉借鉴 dsh-xdpool 面板（PoolCard / CreditDot /
/// credit-strip / 状态 pill），数据源是超级大脑后端 `pool.status` 真实 RPC。
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

  Map<String, dynamic> _data = <String, dynamic>{};
  String _message = '';
  bool _busy = false;

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

  // credit-strip：顶部额度概览横条（xdpool 风格）。
  Widget _strip() {
    final count = (_data['account_count'] ?? 0) as int;
    final accounts = (_data['accounts'] as List<dynamic>? ?? <dynamic>[])
        .whereType<Map>()
        .toList();
    final alive = accounts
        .where((Map a) => (a['dead'] ?? false) != true)
        .length;
    final inRouter = (_data['in_router'] ?? false) == true;
    return Container(
      margin: const EdgeInsets.fromLTRB(16, 12, 16, 8),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: _panel,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: _border),
      ),
      child: Row(children: <Widget>[
        _stripCell('账号', '$count'),
        _stripCell('可用', '$alive'),
        _stripCell('默认模型', (_data['default_model'] ?? '—').toString()),
        const Spacer(),
        _pill(inRouter ? '已入路由' : '未入路由', inRouter ? _ok : _hint),
        const SizedBox(width: 8),
        _pill((_data['enabled'] ?? false) == true ? '号池开' : '号池关',
            (_data['enabled'] ?? false) == true ? _ok : _hint),
        const SizedBox(width: 8),
        _pill((_data['free_lane'] ?? false) == true ? '免密开' : '免密关',
            (_data['free_lane'] ?? false) == true ? _warn : _hint),
      ]),
    );
  }

  Widget _stripCell(String label, String value) => Padding(
        padding: const EdgeInsets.only(right: 22),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: <Widget>[
          Text(label, style: const TextStyle(color: _hint, fontSize: 10)),
          const SizedBox(height: 3),
          Text(value,
              style: const TextStyle(color: Colors.white, fontSize: 13)),
        ]),
      );

  // CreditDot / 状态 pill（xdpool data-tone ok/off 风格）。
  Widget _pill(String text, Color tone) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
        decoration: BoxDecoration(
          color: tone.withValues(alpha: 0.14),
          borderRadius: BorderRadius.circular(999),
          border: Border.all(color: tone.withValues(alpha: 0.5)),
        ),
        child: Text(text, style: TextStyle(color: tone, fontSize: 10)),
      );

  // PoolCard：账号卡片（头像 tile + label + region pill + 状态点 + usage 行）。
  Widget _accountCard(Map account) {
    final dead = (account['dead'] ?? false) == true;
    final dot = dead ? _dead : _ok;
    final cooldowns = (account['model_cooldowns'] as Map<dynamic, dynamic>? ?? {});
    final label = (account['label'] ?? '').toString();
    final region = (account['region'] ?? 'cn').toString();
    return Container(
      margin: const EdgeInsets.fromLTRB(16, 8, 16, 8),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: _panel,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: _border),
      ),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: <Widget>[
        Row(children: <Widget>[
          CircleAvatar(
            radius: 16,
            backgroundColor: dot.withValues(alpha: 0.2),
            child: Text(label.isEmpty ? '?' : label.characters.first,
                style: TextStyle(color: dot, fontSize: 14)),
          ),
          const SizedBox(width: 10),
          Expanded(
            child: Text(label.isEmpty ? _short(account['id']) : label,
                style: const TextStyle(color: Colors.white, fontSize: 13)),
          ),
          _pill(region == 'global' ? '国际版' : '国内版',
              region == 'global' ? _warn : _ok),
          const SizedBox(width: 8),
          // CreditDot：账号状态点
          Container(
            width: 10, height: 10,
            decoration: BoxDecoration(color: dot, shape: BoxShape.circle),
          ),
        ]),
        const SizedBox(height: 8),
        // "Account usage" 行：冷却明细（xdpool 的账号用量行风格）
        Text(
          dead
              ? '凭据冷却中 · 剩余 ${account['dead_remaining_sec']}s'
              : (cooldowns.isEmpty
                  ? '无模型冷却 · 全部可用'
                  : '模型冷却：${cooldowns.entries.map((e) => '${e.key} ${e.value}s').join('，')}'),
          style: TextStyle(
              color: dead ? _dead : _hint,
              fontSize: 11,
              fontFamily: 'monospace'),
        ),
      ]),
    );
  }

  String _short(dynamic value) {
    final text = value?.toString() ?? '';
    return text.length > 8 ? text.substring(0, 8) : text;
  }

  @override
  Widget build(BuildContext context) {
    final accounts = (_data['accounts'] as List<dynamic>? ?? <dynamic>[])
        .whereType<Map>()
        .toList();
    final enabled = (_data['enabled'] ?? false) == true;
    return Scaffold(
      backgroundColor: _bg,
      body: RefreshIndicator(
        onRefresh: _load,
        child: ListView(children: <Widget>[
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 14, 16, 0),
            child: Row(children: <Widget>[
              const Text('号池网关',
                  style: TextStyle(color: Colors.white, fontSize: 18)),
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
              padding: const EdgeInsets.fromLTRB(16, 4, 16, 0),
              child: Text(_message,
                  style: const TextStyle(color: _dead, fontSize: 11)),
            ),
          _strip(),
          if (!enabled)
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 8, 16, 8),
              child: Container(
                padding: const EdgeInsets.all(12),
                decoration: BoxDecoration(
                  color: _panel,
                  borderRadius: BorderRadius.circular(10),
                  border: Border.all(color: _border),
                ),
                child: const Text(
                  '号池未启用：把 config 的 account_pool.enabled 设为 true 后重启后端，\n'
                  '这里会显示真实账号池状态（账号来自 WorkBuddy 登录态或 xdpool 快照）。',
                  style: TextStyle(color: _hint, fontSize: 12, height: 1.5),
                ),
              ),
            )
          else if (accounts.isEmpty)
            const Padding(
              padding: EdgeInsets.all(16),
              child: Text('没有发现账号：本机未找到 WorkBuddy 登录态或 xdpool 快照。',
                  style: TextStyle(color: _hint, fontSize: 12)),
            )
          else
            ...accounts.map(_accountCard),
          const SizedBox(height: 12),
        ]),
      ),
    );
  }
}
