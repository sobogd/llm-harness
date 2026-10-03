import 'package:flutter/material.dart';

import 'state.dart';
import 'ui/root.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final app = AgentApp();
  await app.init();
  runApp(HarnessApp(app: app));
}

class HarnessApp extends StatelessWidget {
  final AgentApp app;
  const HarnessApp({super.key, required this.app});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'harness',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        colorSchemeSeed: const Color(0xFF2E7D6B),
        useMaterial3: true,
      ),
      home: RootScreen(app: app),
    );
  }
}
