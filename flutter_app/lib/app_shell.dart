import 'dart:async';

import 'package:flutter/material.dart';

import 'backend_launcher.dart';
import 'brain_client.dart';
import 'nebula_page.dart';
import 'pages.dart';
import 'pages_extra.dart';
import 'prefs.dart';

/// LivingMemory 风格外壳：左侧十四页导航 + 顶栏（空间/搜索/导出/全屏）+ 底部状态栏。
class AppShell extends StatefulWidget {
  const AppShell({super.key});

  @override
  State<AppShell> createState() => _AppShellState();
}

class _AppShellState extends State<AppShell> {
  late BrainClient _client;
  final _nebulaKey = GlobalKey<NebulaPageState>();
  final TextEditingController _globalSearch = TextEditingController();
  Timer? _statusTimer;

  int _index = 0;
  bool _fullscreen = false;
  String _space = '全部';
  List<String> _spaces = <String>['全部', '情感记忆'];
  Map<String, dynamic> _status = <String, dynamic>{};

  static const List<Map<String, Object>> _nav = <Map<String, Object>>[
    <String, Object>{'name': '星云视图', 'icon': Icons.blur_on},
    <String, Object>{'name': '记忆管理', 'icon': Icons.memory},
    <String, Object>{'name': '召回测试', 'icon': Icons.search},
    <String, Object>{'name': '系统概览', 'icon': Icons.monitor_heart},
    <String, Object>{'name': '展示数据', 'icon': Icons.data_object},
    <String, Object>{'name': '模型配置', 'icon': Icons.smart_toy_outlined},
    <String, Object>{'name': '接口与端口', 'icon': Icons.settings_ethernet},
    <String, Object>{'name': '同步与备份', 'icon': Icons.cloud_sync},
    <String, Object>{'name': '设置', 'icon': Icons.tune},
    <String, Object>{'name': '记忆检查器', 'icon': Icons.fact_check_outlined},
    <String, Object>{'name': '事件时间轴', 'icon': Icons.timeline},
    <String, Object>{'name': '批量导入', 'icon': Icons.upload_file},
    <String, Object>{'name': '全量配置', 'icon': Icons.tune_outlined},
    <String, Object>{'name': '关于', 'icon': Icons.info_outline},
  ];

  @override
  void initState() {
    super.initState();
    final prefs = Prefs.load();
    _client = BrainClient(
      host: prefs['host']?.toString() ?? '127.0.0.1',
      httpPort: prefs['http_port'] as int? ?? 8768,
      wsPort: prefs['ws_port'] as int? ?? 8767,
    );
    _client.connectEvents();
    _loadSpaces();
    _refreshStatus();
    _statusTimer = Timer.periodic(const Duration(seconds: 5), (_) => _refreshStatus());
  }

  Future<void> _loadSpaces() async {
    final data = await _client.spaces();
    if (!mounted) return;
    final tiers = (data['tiers'] as Map<String, dynamic>? ?? <String, dynamic>{}).keys.toList();
    final spaces = <String>['全部', '情感记忆', ...tiers].toSet().toList();
    setState(() {
      _spaces = spaces;
      if (!spaces.contains(_space)) _space = '全部';
    });
  }

  Future<void> _refreshStatus() async {
    final data = await _client.snapshot();
    if (mounted) setState(() => _status = data);
  }

  void _reconnect(String host, int httpPort, int wsPort) {
    _client.dispose();
    setState(() {
      _client = BrainClient(host: host, httpPort: httpPort, wsPort: wsPort);
      _index = 0;
    });
    _client.connectEvents();
  }

  Future<void> _export() async {
    final result = await _client.exportAll();
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
      content: Text(result['error']?.toString() ?? '已导出到 ${result['dashboard']}'),
      duration: const Duration(seconds: 3),
    ));
  }

  @override
  void dispose() {
    _statusTimer?.cancel();
    _client.dispose();
    _globalSearch.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final engines = (_status['engine_health']?['engines'] as List<dynamic>? ?? <dynamic>[]);
    final ok = engines.where((row) => (row as Map<String, dynamic>)['ok'] != false).length;
    return Scaffold(
      backgroundColor: const Color(0xFF05080E),
      body: Row(
        children: <Widget>[
          if (!_fullscreen)
            Container(
              width: 220,
              color: const Color(0xFF070D18),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: <Widget>[
                  const SizedBox(height: 24),
                  const Padding(
                    padding: EdgeInsets.symmetric(horizontal: 18),
                    child: Text('LIVING MEMORY', style: TextStyle(color: Color(0xFFE5BC85), fontSize: 14, fontWeight: FontWeight.bold, letterSpacing: 1.5)),
                  ),
                  const Padding(
                    padding: EdgeInsets.symmetric(horizontal: 18, vertical: 4),
                    child: Text('super-brain v3 · 3D 星云', style: TextStyle(color: Color(0xFF5D7690), fontSize: 10)),
                  ),
                  const SizedBox(height: 16),
                  Expanded(
                    child: ListView.builder(
                      itemCount: _nav.length,
                      itemBuilder: (context, i) => _NavItem(
                        icon: _nav[i]['icon']! as IconData,
                        label: _nav[i]['name']! as String,
                        selected: _index == i,
                        onTap: () => setState(() => _index = i),
                      ),
                    ),
                  ),
                  const Padding(
                    padding: EdgeInsets.all(14),
                    child: Text('拖拽翻滚 · 滚轮缩放\n双击复位 · 检索触发星爆',
                        style: TextStyle(color: Color(0xFF3E5470), fontSize: 10, height: 1.6)),
                  ),
                ],
              ),
            ),
          Expanded(
            child: Column(
              children: <Widget>[
                // ── 顶栏 ────────────────────────────────────────────────
                Container(
                  height: 52,
                  padding: const EdgeInsets.symmetric(horizontal: 18),
                  decoration: const BoxDecoration(border: Border(bottom: BorderSide(color: Color(0xFF142238)))),
                  child: Row(
                    children: <Widget>[
                      DropdownButton<String>(
                        value: _space,
                        dropdownColor: const Color(0xFF0A1424),
                        style: const TextStyle(color: Color(0xFFBFD8F5), fontSize: 11),
                        underline: const SizedBox.shrink(),
                        items: _spaces.map((space) => DropdownMenuItem<String>(
                              value: space, child: Text(space))).toList(),
                        onChanged: (value) {
                          if (value == null) return;
                          setState(() => _space = value);
                          _nebulaKey.currentState?.applySpace(value);
                        },
                      ),
                      const SizedBox(width: 14),
                      IconButton(
                        tooltip: '返回全景',
                        icon: const Icon(Icons.home_outlined, color: Color(0xFF8FA6C4), size: 18),
                        onPressed: () => _nebulaKey.currentState?.resetView(),
                      ),
                      Expanded(
                        child: TextField(
                          controller: _globalSearch,
                          style: const TextStyle(color: Colors.white, fontSize: 12),
                          decoration: const InputDecoration(
                            hintText: '搜索记忆并激活星爆…',
                            hintStyle: TextStyle(color: Color(0xFF63788F), fontSize: 11),
                            border: InputBorder.none,
                            prefixIcon: Icon(Icons.search, color: Color(0xFF4FC3F7), size: 16),
                          ),
                          onSubmitted: (value) {
                            setState(() => _index = 0);
                            _nebulaKey.currentState?.search(value);
                          },
                        ),
                      ),
                      IconButton(tooltip: '导出数据', icon: const Icon(Icons.download_outlined, color: Color(0xFF8FA6C4), size: 18), onPressed: _export),
                      IconButton(
                        tooltip: '全屏',
                        icon: const Icon(Icons.fullscreen, color: Color(0xFF8FA6C4), size: 18),
                        onPressed: () => setState(() => _fullscreen = !_fullscreen),
                      ),
                      const SizedBox(width: 6),
                      const Icon(Icons.circle, color: Color(0xFF3DDC84), size: 7),
                      const SizedBox(width: 6),
                      Text('${_client.host}:${_client.httpPort}', style: const TextStyle(color: Color(0xFF6B839F), fontSize: 10)),
                    ],
                  ),
                ),
                // ── 主区 ────────────────────────────────────────────────
                Expanded(
                  child: IndexedStack(
                    index: _index,
                    children: <Widget>[
                      NebulaPage(key: _nebulaKey, client: _client),
                      MemoriesPage(client: _client),
                      RecallPage(client: _client),
                      SystemPage(client: _client),
                      DataPage(client: _client),
                      ModelPage(client: _client),
                      InterfacePage(client: _client),
                      SyncPage(client: _client),
                      SettingsPage(client: _client, onReconnect: _reconnect),
                      MemoryInspectorPage(client: _client),
                      TimelinePage(client: _client),
                      ImportPage(client: _client),
                      FullConfigPage(client: _client),
                      const AboutPage(),
                    ],
                  ),
                ),
                // ── 底部状态栏 ──────────────────────────────────────────
                Container(
                  height: 28,
                  padding: const EdgeInsets.symmetric(horizontal: 18),
                  color: const Color(0xFF070D18),
                  child: Row(
                    children: <Widget>[
                      Text(
                        '引擎 ${engines.isEmpty ? '—' : '$ok/${engines.length}'} · '
                        '记忆 ${_status['memory_stream']?['count'] ?? 0} · '
                        '空间 $_space · 待重放 ${_status['sync_status']?['pending_events'] ?? 0}',
                        style: const TextStyle(color: Color(0xFF6B839F), fontSize: 10),
                      ),
                      const Spacer(),
                      ValueListenableBuilder<String>(
                        valueListenable: backendStatus,
                        builder: (context, value, _) => Text(
                          'WebSocket RPC · $value',
                          style: const TextStyle(color: Color(0xFF3E5470), fontSize: 10),
                        ),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _NavItem extends StatelessWidget {
  const _NavItem({required this.icon, required this.label, required this.selected, required this.onTap});
  final IconData icon;
  final String label;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) => InkWell(
        onTap: onTap,
        child: Container(
          margin: const EdgeInsets.symmetric(horizontal: 10, vertical: 2),
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
          decoration: BoxDecoration(
            color: selected ? const Color(0xFF123049) : Colors.transparent,
            borderRadius: BorderRadius.circular(8),
          ),
          child: Row(
            children: <Widget>[
              Icon(icon, size: 16, color: selected ? const Color(0xFF4FC3F7) : const Color(0xFF6B839F)),
              const SizedBox(width: 12),
              Text(label, style: TextStyle(color: selected ? Colors.white : const Color(0xFF8FA6C4), fontSize: 12)),
            ],
          ),
        ),
      );
}
