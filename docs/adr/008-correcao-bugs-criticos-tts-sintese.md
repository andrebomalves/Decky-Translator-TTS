# ADR-008: Correção de bugs críticos na síntese Piper e chamadas backend

**Status:** Aceito
**Data:** 2026-08-23
**Decisor:** andre-bom + Hermes Agent
**Relacionado:** SDD specs/TTS-001.md, ADR-002

## Contexto

Após instalação no Steam Deck real (192.168.0.235), o TTS não produzia áudio. Teste de voz falhava com `'dict' object has no attribute 'strip'`. Síntese Piper lançava `InvalidArgument: Invalid rank for input: scales`. Logs mostravam `Speaker: provider indisponível e arquivo não existe`.

## Problemas identificados e corrigidos

### Bug 1: `test_tts_voice` recebe dict em vez de string
- **Causa:** Frontend chama `call('test_tts_voice', { text: testText })`. Decky Loader passa o dict como primeiro argumento posicional. Backend esperava `text: str`.
- **Correção:** Adicionado `isinstance(text, dict)` check no início do método para extrair `text.get("text", "")`.

### Bug 2: `auto_read` desativado por padrão
- **Causa:** `_tts_auto_read: bool = False` — traduções eram armazenadas mas nunca faladas automaticamente.
- **Correção:** Default alterado para `True`.

### Bug 3: Piper ONNX — `scales` com rank errado
- **Causa:** `feed[name] = [[1.0 / self.speed]]` criava array 2D `[1, 1]` mas modelo espera 1D `[3]` (length_scale, noise_scale, noise_w).
- **Correção:** Arrays numpy com dtype explícito: `np.array([1.0/speed, 0.667, 0.8], dtype=np.float32)`.

### Bug 4: Piper ONNX — `input` recebia valores de `scales`
- **Causa:** Condições na ordem errada. `"input" in "input"` era True, então `input` caía no primeiro `if` (que era para `input_ids`). O `else` (scales) nunca era atingido para o campo `scales`.
- **Correção:** Reordenado: checa `"length"` antes de `"id"` antes do `else`.

### Bug 5: espeak-ng `--phonout=-` não funcionava
- **Causa:** Flag `--phonout=-` não produz saída na versão 1.52.0 do espeak-ng no SteamOS.
- **Correção:** Trocado para flag `-x` que saída fonemas no formato esperado pelo Piper.

### Bug 6: Variável `name` com shadowing no loop
- **Causa:** Loop interno `for name in input_names:` colidia com variável `name` do loop externo, causando atribuição incorreta no dict `feed`.
- **Correção:** Variável renomeada para `n` com `nl = n.lower()` para clareza.

## Consequências
- Positivas: TTS funcional no Steam Deck real, síntese Piper produce áudio correto.
- Negativas: Nenhuma.

## Validação
- Síntese direta: `piper.synthesize("Olá, teste de voz.", "/tmp/test.wav")` → WAV 22.572 bytes.
- Reprodução: `pw-play /tmp/test.wav` → áudio audível.
- Plugin Decky: carrega sem erros, `TTS initialized - provider: piper`.
- Teste de voz via UI: botão "Testar" funciona sem erro.
