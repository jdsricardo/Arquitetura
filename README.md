# Ledger de contas

API em Python com um painel web para registrar créditos, débitos e consultar o saldo atual ou histórico de uma conta. Os dados ficam em SQLite.

As escolhas de implementação estão em [decisoes.txt](decisoes.txt).

## Como rodar

É necessário Python 3.10 ou superior, com SQLite disponível. Não há dependências externas.

Na pasta do projeto:

```sh
python server.py
```

Abra http://127.0.0.1:8000. Crie uma conta, faça um crédito e registre um débito. Para consultar o saldo em outro momento, preencha a data no painel. Encerre o servidor com `Ctrl+C`.

O arquivo `ledger.db` é criado automaticamente e mantém os dados entre execuções. Para usar outro caminho, defina a variável de ambiente `LEDGER_DB`; a pasta de destino precisa existir. O banco local e os caches de Python ficam fora do versionamento pelo `.gitignore`.

## API

| Método | Rota | Operação |
| --- | --- | --- |
| POST | `/accounts` | Criar conta |
| POST | `/accounts/{id}/transactions` | Registrar crédito ou débito |
| GET | `/accounts/{id}/balance` | Consultar saldo |
| GET | `/accounts/{id}/balance?at=...` | Consultar saldo em um instante |
| GET | `/health` | Verificar se o servidor HTTP responde |

Exemplo no PowerShell:

```powershell
$base = "http://127.0.0.1:8000"
$conta = Invoke-RestMethod -Method Post -Uri "$base/accounts" `
  -ContentType "application/json" `
  -Body '{"customer_id":"cliente-123","currency":"BRL"}'
$id = $conta.id

Invoke-RestMethod -Method Post -Uri "$base/accounts/$id/transactions" `
  -ContentType "application/json" `
  -Headers @{ "Idempotency-Key" = "deposito-001" } `
  -Body '{"direction":"credit","amount":"125.90","description":"Deposito"}'

Invoke-RestMethod "$base/accounts/$id/balance"
```

Para debitar, use `"direction":"debit"` e uma nova chave. O valor deve ser uma string positiva com ponto decimal e até duas casas. A resposta usa centavos inteiros: `12590` equivale a R$ 125,90 em BRL. Débitos sem saldo suficiente retornam `409`.

Se uma requisição de lançamento falhar ou perder a resposta, repita com a mesma `Idempotency-Key` e os mesmos dados. A API devolve o lançamento já registrado; mudar os dados com a mesma chave retorna `409`. A criação de contas não tem esse tratamento.

O parâmetro `at` aceita data ISO 8601 com fuso, por exemplo `2026-11-20T12:00:00-03:00`. Codifique o valor na URL. A consulta inclui lançamentos até esse instante; datas futuras são recusadas. Sem `at`, retorna o saldo atual. As consultas são por conta.

## Arquivos

- `ledger.py`: regras de movimentação e acesso ao SQLite.
- `server.py`: API HTTP e validação das requisições.
- `index.html`: painel web.
- `decisoes.txt`: motivos das escolhas e cuidados de operação.

O servidor aceita conexões apenas em `127.0.0.1` e não possui autenticação. Use dados fictícios na execução local.
