import 'package:flutter/material.dart';

import 'brain_client.dart';
import 'nebula_page.dart';
import 'pages.dart';
import 'prefs.dart';

class AppShell extends StatefulWidget {
  const AppShell({super.key});

  @override
  State<AppShell> createState() => _AppShellState();
}

class _AppShellState extends State<AppShell> {
  late BrainClient _client;
  int _index = 0;
  String _title = '星云视图';

  static const List<Map<String, Object>> _nav = <Map<String, Object>>[
    <String, Object>{'name': '星云视图', 'icon': Icons.blur_on},
    <String, Object>{'name': '记忆管理', 'icon': Icons.memory},
    <String, Object>{'name': '召回测试', 'icon': Icons.search},
    <String, Object>{'name': '接口与端口', 'icon': Icons.settings_ethernet},
    <String, Object>{'name': '设置', 'icon': Icons.tune},
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
  }

  void _reconnect(String host, int httpPort, int wsPort) {
    _client.dispose();
    setState(() {
      _client = BrainClient(host: host, httpPort: httpPort, wsPort: wsPort);
      _index = 0;
      _title = '星云视图';
    });
  }

  @override
  void dispose() {
    _client.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: const Color(0xFF05080E),
      body: Row(
        children: <Widget>[
          // ── 侧栏导航 ────────────────────────────────────────────────────
          Container(
            width: 220,
            color: const Color(0xFF070D18),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: <Widget>[
                const SizedBox(height: 26),
                const Padding(
                  padding: EdgeInsets.symmetric(horizontal: 18),
                  child: Text('SUPER BRAIN', style: TextStyle(color: Color(0xFF4FC3F7), fontSize: 15, fontWeight: FontWeight.bold, letterSpacing: 1.6)),
                ),
                const Padding(
                  padding: EdgeInsets.symmetric(horizontal: 18, vertical: 4),
                  child: Text('v3 · 记忆星云', style: TextStyle(color: Color(0xFF5D7690), fontSize: 10)),
                ),
                const SizedBox(height: 22),
                for (var i = 0; i < _nav.length; i++)
                  _NavItem(
                    icon: _nav[i]['icon']! as IconData,
                    label: _nav[i]['name']! as String,
                    selected: _index == i,
                    onTap: () => setState(() {
                      _index = i;
                      _title = _nav[i]['name']! as String;
                    }),
                  ),
                const Spacer(),
                const Padding(
                  padding: EdgeInsets.all(16),
                  child: Text('拖拽翻滚 · 滚轮缩放\n双击复位视角', style: TextStyle(color: Color(0xFF3E5470), fontSize: 10, height: 1.6)),
                ),
              ],
            ),
          ),
          // ── 主区 ───────────────────────────────────────────────────────
          Expanded(
            child: Column(
              children: <Widget>[
                Container(
                  height: 54,
                  padding: const EdgeInsets.symmetric(horizontal: 22),
                  decoration: const BoxDecoration(border: Border(bottom: BorderSide(color: Color(0xFF142238)))),
                  child: Row(
                    children: <Widget>[
                      Text(_title, style: const TextStyle(color: Colors.white, fontSize: 15, fontWeight: FontWeight.w500)),
                      const Spacer(),
                      const Icon(Icons.circle, color: Color(0xFF3DDC84), size: 8),
                      const SizedBox(width: 6),
                      Text('${_client.host}:${_client.httpPort}', style: const TextStyle(color: Color(0xFF6B839F), fontSize: 11)),
                    ],
                  ),
                ),
                Expanded(
                  child: IndexedStack(
                    index: _index,
                    children: <Widget>[
                      NebulaPage(key: ValueKey<String>('nebula-${_client.base}'), client: _client),
                      MemoriesPage(client: _client),
                      RecallPage(client: _client),
                      InterfacePage(client: _client),
                      SettingsPage(client: _client, onReconnect: _reconnect),
                      const AboutPage(),
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
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 11),
          decoration: BoxDecoration(
            color: selected ? const Color(0xFF123049) : Colors.transparent,
            borderRadius: BorderRadius.circular(8),
          ),
          child: Row(
            children: <Widget>[
              Icon(icon, size: 17, color: selected ? const Color(0xFF4FC3F7) : const Color(0xFF6B839F)),
              const SizedBox(width: 12),
              Text(label, style: TextStyle(color: selected ? Colors.white : const Color(0xFF8FA6C4), fontSize: 12.5)),
            ],
          ),
        ),
      );
}
