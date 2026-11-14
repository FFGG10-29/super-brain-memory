import 'dart:io';

import 'package:flutter/material.dart';

import 'brain_client.dart';
import 'pages.dart' show PageFrame;
import 'win32.dart';

const Color _hint = Color(0xFF8FA6C4);

/// 桌面能力：托盘驻留 / 开机自启 / 数据目录一键迁移。
/// 密钥已拆分：GitHub PAT→「同步与备份」，模型 API Key→「模型配置」，均存系统钥匙串。
/// 这三项全部由后端 core.* RPC 提供，Flutter 完全等价。
class SystemFeaturesPage extends StatefulWidget {
  const SystemFeaturesPage({super.key, required this.client});
  final BrainClient client;

  @override
  State<SystemFeaturesPage> createState() => _SystemFeaturesPageState();
}

class _SystemFeaturesPageState extends State<SystemFeaturesPage> {
  final TextEditingController _target = TextEditingController();
  Map<String, dynamic> _tray = <String, dynamic>{};
  Map<String, dynamic> _autostart = <String, dynamic>{};
  Map<String, dynamic> _data = <String, dynamic>{};
  String _message = '—';
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    _loadAll();
  }

  @override
  void dispose() {
    _target.dispose();
    super.dispose();
  }

  Future<void> _loadAll() async {
    setState(() => _busy = true);
    final results = await Future.wait<dynamic>(<Future<dynamic>>[
      widget.client.trayStatus(),
      widget.client.autostartStatus(),
      widget.client.dataStatus(),
    ]);
    if (!mounted) return;
    setState(() {
      _tray = _map(results[0]);
      _autostart = _map(results[1]);
      _data = _map(results[2]);
      _busy = false;
      _message = '状态已刷新';
    });
    trayStatus.value = _tray['running'] == true
        ? '托盘已启用 · 关窗隐藏到托盘，服务继续运行'
        : '托盘未启用 · 关窗直接退出';
  }

  static Map<String, dynamic> _map(dynamic value) =>
      value is Map<String, dynamic> ? value : <String, dynamic>{'error': '$value'};

  void _say(String text) {
    if (mounted) setState(() => _message = text);
  }

  Future<void> _toggleTray(bool enabled) async {
    final result = await widget.client.setTray(enabled);
    final ok = result['ok'] == true || result['running'] == true;
    if (ok && enabled) Win32Window.setTrayMode(true);
    if (!enabled) Win32Window.setTrayMode(false);
    _say(result['message']?.toString() ?? result['error']?.toString() ?? '托盘状态已更新');
    trayStatus.value = enabled ? '托盘已启用 · 关窗隐藏' : '托盘已停用 · 关窗直接退出';
    await _loadAll();
  }

  Future<void> _toggleAutostart(bool enabled) async {
    final result = await widget.client.setAutostart(enabled, Platform.resolvedExecutable);
    _say(result['message']?.toString() ?? result['error']?.toString() ?? '自启状态已更新');
    await _loadAll();
  }

  Future<void> _migrate() async {
    final target = _target.text.trim();
    if (target.isEmpty) {
      _say('请先填写目标目录');
      return;
    }
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        backgroundColor: const Color(0xFF0A1424),
        title: const Text('确认迁移数据目录', style: TextStyle(color: Colors.white, fontSize: 15)),
        content: Text(
          '将把 ${_data['current']}（${_data['files']} 个文件 / ${_data['size_mb']} MB）复制到\n$target\n\n'
          '默认保留原目录作为回退；复制时仍在使用中的锁文件会被跳过。\n'
          '迁移后需重启后端才会切换到新目录。',
          style: const TextStyle(color: _hint, fontSize: 12, height: 1.6),
        ),
        actions: <Widget>[
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('取消')),
          FilledButton(onPressed: () => Navigator.pop(context, true), child: const Text('开始复制')),
        ],
      ),
    );
    if (confirmed != true) return;
    _say('正在复制，请稍候…');
    final result = await widget.client.migrateData(target);
    _say(result['message']?.toString() ?? result['error']?.toString() ?? '迁移结束');
    await _loadAll();
  }

  Future<void> _resetPointer() async {
    final result = await widget.client.resetDataPointer();
    _say(result['message']?.toString() ?? '已恢复默认数据目录');
    await _loadAll();
  }

  @override
  Widget build(BuildContext context) => PageFrame(
        title: '桌面能力',
        subtitle: '托盘驻留 / 单实例 / 开机自启 / 系统钥匙串 / 数据目录迁移 —— 全部由后端 RPC 提供',
        child: ListView(
          children: <Widget>[
            Text(_busy ? '正在读取状态…' : _message,
                style: const TextStyle(color: _hint, fontSize: 11)),
            const SizedBox(height: 10),
            _card('系统托盘与单实例', <Widget>[
              _row('托盘可用', _tray['available'] == true ? '是' : '否（仅 Windows）'),
              _row('当前状态', _tray['running'] == true ? '运行中 · 关窗隐藏' : '未运行 · 关窗退出'),
              if ((_tray['error']?.toString() ?? '').isNotEmpty)
                _row('错误', _tray['error'].toString()),
              _row('单实例', '已启用（本地端口锁，第二个实例唤起已有窗口后自动退出）'),
              const SizedBox(height: 10),
              Wrap(
                spacing: 8,
                children: <Widget>[
                  FilledButton(
                    onPressed: () => _toggleTray(true),
                    child: const Text('启用托盘'),
                  ),
                  OutlinedButton(
                    onPressed: () => _toggleTray(false),
                    child: const Text('停用托盘'),
                  ),
                ],
              ),
              const SizedBox(height: 8),
              const Text('托盘菜单：打开主界面 / 记忆星云 / 事件时间轴 / 完全退出；双击图标回到前台。'
                  ' 关窗只隐藏窗口，后端六引擎与 MCP 网关继续运行。',
                  style: TextStyle(color: Color(0xFF5D7690), fontSize: 10, height: 1.6)),
            ]),
            const SizedBox(height: 12),
            _card('开机自启', <Widget>[
              _row('当前状态', _autostart['enabled'] == true ? '已启用' : '未启用'),
              _row('启动命令', _autostart['command']?.toString() ?? '—'),
              const SizedBox(height: 10),
              SwitchListTile(
                title: const Text('登录 Windows 后自动驻留托盘', style: TextStyle(color: _hint, fontSize: 12)),
                value: _autostart['enabled'] == true,
                onChanged: (value) => _toggleAutostart(value),
                contentPadding: EdgeInsets.zero,
              ),
              const Text('写入 HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run，'
                  ' 不需要管理员权限；命令带 --tray，开机不弹窗。',
                  style: TextStyle(color: Color(0xFF5D7690), fontSize: 10, height: 1.6)),
            ]),
            const SizedBox(height: 12),
            // 密钥保管已拆分：GitHub PAT 在「同步与备份」页；模型 API Key 在「模型配置」页。
            _card('数据目录一键迁移', <Widget>[
              _row('当前目录', _data['current']?.toString() ?? '—'),
              _row('文件 / 大小', '${_data['files'] ?? '—'} 个 / ${_data['size_mb'] ?? '—'} MB'),
              _row('迁移指针', (_data['pointer']?.toString() ?? '').isEmpty ? '无（使用默认）' : _data['pointer'].toString()),
              _row('环境变量覆盖', (_data['env_override']?.toString() ?? '').isEmpty ? '无' : _data['env_override'].toString()),
              const SizedBox(height: 10),
              TextField(
                controller: _target,
                style: const TextStyle(color: Colors.white, fontSize: 12),
                decoration: const InputDecoration(
                  labelText: '目标目录（例如 D:\\Super-Brain\\data）',
                  labelStyle: TextStyle(color: _hint, fontSize: 11),
                  border: OutlineInputBorder(),
                  isDense: true,
                ),
              ),
              const SizedBox(height: 10),
              Wrap(
                spacing: 8,
                children: <Widget>[
                  FilledButton(onPressed: _migrate, child: const Text('复制到目标目录')),
                  OutlinedButton(onPressed: _resetPointer, child: const Text('恢复默认目录')),
                  OutlinedButton(onPressed: _loadAll, child: const Text('刷新状态')),
                ],
              ),
              const SizedBox(height: 8),
              const Text('默认只复制、不删除原目录；复制期间后端仍在写，被占用的锁文件会跳过。'
                  ' 写入 data_pointer.json 后需重启后端（或重启应用）才生效。',
                  style: TextStyle(color: Color(0xFF5D7690), fontSize: 10, height: 1.6)),
            ]),
          ],
        ),
      );

  Widget _card(String title, List<Widget> children) => Container(
        padding: const EdgeInsets.all(16),
        decoration: BoxDecoration(
          color: const Color(0xFF0A1424),
          borderRadius: BorderRadius.circular(10),
          border: Border.all(color: const Color(0xFF142238)),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Text(title, style: const TextStyle(color: Colors.white, fontSize: 14)),
            const SizedBox(height: 12),
            ...children,
          ],
        ),
      );

  Widget _row(String label, String value) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 3),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            SizedBox(width: 110, child: Text(label, style: const TextStyle(color: _hint, fontSize: 11))),
            Expanded(child: SelectableText(value, style: const TextStyle(color: Colors.white, fontSize: 11))),
          ],
        ),
      );
}
