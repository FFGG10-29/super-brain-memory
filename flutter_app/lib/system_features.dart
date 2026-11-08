import 'dart:io';

import 'package:flutter/material.dart';

import 'brain_client.dart';
import 'pages.dart' show PageFrame;
import 'win32.dart';

const Color _hint = Color(0xFF8FA6C4);

/// 桌面能力：托盘驻留 / 开机自启 / 密钥保管 / 数据目录一键迁移。
/// 这四项原先只在 Qt 前端里有，现在全部由后端 core.* RPC 提供，Flutter 完全等价。
class SystemFeaturesPage extends StatefulWidget {
  const SystemFeaturesPage({super.key, required this.client});
  final BrainClient client;

  @override
  State<SystemFeaturesPage> createState() => _SystemFeaturesPageState();
}

class _SystemFeaturesPageState extends State<SystemFeaturesPage> {
  final TextEditingController _target = TextEditingController();
  final Map<String, TextEditingController> _secretInputs = <String, TextEditingController>{
    'github_pat': TextEditingController(),
    'deepseek_api_key': TextEditingController(),
    'openai_api_key': TextEditingController(),
  };
  Map<String, dynamic> _tray = <String, dynamic>{};
  Map<String, dynamic> _autostart = <String, dynamic>{};
  Map<String, dynamic> _secrets = <String, dynamic>{};
  Map<String, dynamic> _data = <String, dynamic>{};
  String _message = '—';
  bool _busy = false;

  static const Map<String, String> _secretLabels = <String, String>{
    'github_pat': 'GitHub PAT（同步仓库用）',
    'deepseek_api_key': 'DeepSeek API Key',
    'openai_api_key': 'OpenAI / 兼容端点 API Key',
  };

  @override
  void initState() {
    super.initState();
    _loadAll();
  }

  @override
  void dispose() {
    _target.dispose();
    for (final controller in _secretInputs.values) {
      controller.dispose();
    }
    super.dispose();
  }

  Future<void> _loadAll() async {
    setState(() => _busy = true);
    final results = await Future.wait<dynamic>(<Future<dynamic>>[
      widget.client.trayStatus(),
      widget.client.autostartStatus(),
      widget.client.secretsStatus(),
      widget.client.dataStatus(),
    ]);
    if (!mounted) return;
    setState(() {
      _tray = _map(results[0]);
      _autostart = _map(results[1]);
      _secrets = _map(results[2]);
      _data = _map(results[3]);
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

  Future<void> _saveSecret(String account) async {
    final value = _secretInputs[account]?.text.trim() ?? '';
    if (value.isEmpty) {
      _say('输入为空，未改动 $account');
      return;
    }
    final result = await widget.client.writeSecret(account, value);
    _secretInputs[account]?.clear();
    _say('${result['ok'] == true ? '已保存' : '保存失败'} $account · 后端 ${result['backend'] ?? '—'}');
    await _loadAll();
  }

  Future<void> _deleteSecret(String account) async {
    await widget.client.deleteSecret(account);
    _say('已删除 $account');
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
        subtitle: '托盘驻留 / 单实例 / 开机自启 / 系统钥匙串 / 数据目录迁移 —— 全部由后端 RPC 提供，不再依赖 Qt',
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
            _card('密钥保管', <Widget>[
              _row('后端', _secrets['backend']?.toString() ?? '—'),
              const SizedBox(height: 6),
              const Text('优先系统钥匙串，不可用则 DPAPI 加密文件（仅当前 Windows 用户可解）。'
                  ' 页面只显示「是否已保存」，绝不回显明文。',
                  style: TextStyle(color: Color(0xFF5D7690), fontSize: 10, height: 1.6)),
              const SizedBox(height: 10),
              for (final entry in _secretInputs.entries) ...<Widget>[
                Builder(builder: (context) {
                  final saved = ((_secrets['accounts'] as Map<String, dynamic>? ?? <String, dynamic>{})[entry.key] == true);
                  return Padding(
                    padding: const EdgeInsets.symmetric(vertical: 6),
                    child: Row(
                      children: <Widget>[
                        SizedBox(
                          width: 190,
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: <Widget>[
                              Text(_secretLabels[entry.key] ?? entry.key,
                                  style: const TextStyle(color: _hint, fontSize: 11)),
                              Text(saved ? '● 已保存' : '○ 未保存',
                                  style: TextStyle(color: saved ? const Color(0xFF3DDC84) : const Color(0xFF5D7690), fontSize: 10)),
                            ],
                          ),
                        ),
                        Expanded(
                          child: TextField(
                            controller: entry.value,
                            obscureText: true,
                            style: const TextStyle(color: Colors.white, fontSize: 12),
                            decoration: const InputDecoration(
                              hintText: '输入新值（留空不修改）',
                              hintStyle: TextStyle(color: Color(0xFF5D7690), fontSize: 10),
                              border: OutlineInputBorder(),
                              isDense: true,
                              contentPadding: EdgeInsets.symmetric(horizontal: 10, vertical: 10),
                            ),
                          ),
                        ),
                        const SizedBox(width: 8),
                        OutlinedButton(
                          onPressed: () => _saveSecret(entry.key),
                          child: const Text('保存'),
                        ),
                        const SizedBox(width: 6),
                        IconButton(
                          tooltip: '删除',
                          onPressed: () => _deleteSecret(entry.key),
                          icon: const Icon(Icons.delete_outline, color: _hint, size: 18),
                        ),
                      ],
                    ),
                  );
                }),
              ],
            ]),
            const SizedBox(height: 12),
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
