import 'package:flutter/material.dart';

import 'app_shell.dart';
import 'prefs.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  Prefs.load();
  runApp(const SuperBrainApp());
}

class SuperBrainApp extends StatelessWidget {
  const SuperBrainApp({super.key});

  @override
  Widget build(BuildContext context) => MaterialApp(
        title: 'Super Brain v3',
        debugShowCheckedModeBanner: false,
        theme: ThemeData(
          brightness: Brightness.dark,
          scaffoldBackgroundColor: const Color(0xFF05080E),
          colorScheme: const ColorScheme.dark(
            primary: Color(0xFF4FC3F7),
            secondary: Color(0xFF7FE9FF),
            surface: Color(0xFF0A1424),
          ),
          fontFamily: 'Microsoft YaHei UI',
        ),
        home: const AppShell(),
      );
}
