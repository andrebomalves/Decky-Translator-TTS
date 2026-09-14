# ADR-009: Dropdowns para seleção múltipla no painel TTS

**Status:** Aceito
**Data:** 2026-08-23
**Decisor:** andre-bom + Hermes Agent
**Relacionado:** SDD specs/TTS-001.md

## Contexto

O painel de configurações TTS usava botões customizados (estilo "card selector") para escolhas de múltipla opção: Provedor de Voz, Voz e Compatibilidade. O restante do Decky Translator usa `DFL.Dropdown` para seleções similares, criando inconsistência visual e pior experiência de uso (botões ocupam mais espaço, difícil navegação com gamepad).

## Decisão

Substituir os 3 seletores por `DFL.Dropdown` do Decky Frontend Library:

| Campo | Antes | Depois |
|-------|-------|--------|
| Provedor de Voz | 3 botões lado a lado | `DFL.Dropdown` com `rgOptions` |
| Voz | Lista de botões empilhados | `DFL.Dropdown` com `rgOptions` |
| Compatibilidade | Lista de botões empilhados | `DFL.Dropdown` com `rgOptions` |

### API utilizada
```javascript
DFL.Dropdown({
    rgOptions: [{ label: '...', data: '...' }],
    selectedOption: options.find(o => o.data === currentValue),
    onChange: (opt) => updateSetting(key, opt.data)
})
```

## Consequências
- Positivas: Consistência visual com resto do plugin, melhor navegação com gamepad, menos espaço vertical.
- Negativas: Nenhuma.

## Validação
- Abaixo do Steam Deck → Decky Translator → Configurações TTS → todos os 3 campos mostram dropdown nativo.
- Seleção de opção atualiza configuração corretamente.
- Dropdown de Voz muda conteúdo ao trocar de provedor (Piper vs Edge).
