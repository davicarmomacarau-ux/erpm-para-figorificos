from dataclasses import dataclass
from datetime import datetime, timedelta
import os
import time
import json
import tkinter as tk
from tkinter import messagebox, ttk
from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, sessionmaker

try:
  from flask import Flask, jsonify, Response, request
except Exception:
  Flask = None
  jsonify = None
  Response = None
  request = None


# -----------------------------
# Robustez enterprise-grade
# -----------------------------
@dataclass(frozen=True)
class ProblemDetails:
  type: str
  title: str
  status: int
  detail: str
  instance: str

  def to_json(self) -> str:
    return json.dumps({
        'type': self.type,
        'title': self.title,
        'status': self.status,
        'detail': self.detail,
        'instance': self.instance,
    })


class DomainViolationException(Exception):
  """Exceção de domínio com payload RFC 7807."""

  def __init__(self, title: str, detail: str, instance: str = 'erp/frigorifico'):
    self.problem = ProblemDetails(
        type='https://erp.frigorifico.br/problems/domain-violation',
        title=title,
        status=422,
        detail=detail,
        instance=instance,
    )
    super().__init__(detail)


class AuditLogEntry:
  """Estrutura em memória de auditoria de alteração crítica."""

  def __init__(self, entity: str, entity_id: int, action: str, actor: str,
               previous_state: dict, new_state: dict, version: int = 1):
    self.entity = entity
    self.entity_id = entity_id
    self.action = action
    self.actor = actor
    self.previous_state = previous_state
    self.new_state = new_state
    self.version = version
    self.created_at = datetime.utcnow()

  def as_dict(self):
    return {
        'entity': self.entity,
        'entity_id': self.entity_id,
        'action': self.action,
        'actor': self.actor,
        'previous_state': self.previous_state,
        'new_state': self.new_state,
        'version': self.version,
        'created_at': self.created_at.isoformat(),
    }


class AuditTrailService:
  """Log de auditoria de entidades críticas com JSON diff e versionamento."""

  def __init__(self):
    self.logs = []

  def record(self, entity: str, entity_id: int, action: str,
             actor: str, previous_state: dict, new_state: dict, version: int = 1):
    entry = AuditLogEntry(entity, entity_id, action, actor,
                          previous_state, new_state, version)
    self.logs.append(entry)
    return entry.as_dict()


class RetryPolicy:
  """Retry para integrações do ERP com resiliencia industrial."""

  def __init__(self, max_retries=3, base_delay_seconds=1.0):
    self.max_retries = max_retries
    self.base_delay_seconds = base_delay_seconds

  def execute(self, operation, *args, **kwargs):
    attempt = 0
    while True:
      try:
        return operation(*args, **kwargs)
      except Exception as exc:
        attempt += 1
        if attempt >= self.max_retries:
          raise exc
        time.sleep(self.base_delay_seconds * attempt)


class CircuitBreaker:
  """Circuit breaker simples para balanças, SIF e integrações externas."""

  def __init__(self, failure_threshold=3, reset_timeout_seconds=15):
    self.failure_threshold = failure_threshold
    self.reset_timeout_seconds = reset_timeout_seconds
    self.failures = 0
    self.open_until = None

  def allow(self):
    if self.open_until is not None and datetime.utcnow() < self.open_until:
      return False
    if self.open_until is not None and datetime.utcnow() >= self.open_until:
      self.failures = 0
      self.open_until = None
    return True

  def record_success(self):
    self.failures = 0
    self.open_until = None

  def record_failure(self):
    self.failures += 1
    if self.failures >= self.failure_threshold:
      self.open_until = datetime.utcnow() + timedelta(seconds=self.reset_timeout_seconds)


class RbacService:
  """Controle de acesso por módulo, funcionalidade e unidade industrial."""

  def __init__(self):
    self.permissions = {
        'admin': {'*'},
        'sif': {'sif:read', 'sif:write'},
        'desossa': {'desossa:read', 'desossa:write'},
        'expedicao': {'expedicao:read', 'expedicao:write'},
    }

  def authorize(self, role: str, module_action: str) -> bool:
    if role not in self.permissions:
      return False
    allowed = self.permissions[role]
    if '*' in allowed:
      return True
    return module_action in allowed


# -----------------------------
# DTOs e parser de balança industrial
# -----------------------------
@dataclass(frozen=True)
class ScaleReading:
  pesoLiquido: float
  pesoBruto: float
  tara: float
  estavel: bool
  unidade: str = 'KG'


class ScaleParser:
  """Parser de linhas de balança padrão Toledo/Filizola."""

  @staticmethod
  def parse(line: str) -> ScaleReading:
    payload = {}
    normalized = line.strip()
    if normalized.startswith('@'):
      normalized = normalized[1:]

    for token in normalized.split(';'):
      if '=' not in token:
        continue
      key, val = token.split('=', 1)
      payload[key.strip().upper()] = val.strip()

    bruto = float(payload.get('BRUTO', 0.0) or 0.0)
    tara = float(payload.get('TARA', 0.0) or 0.0)
    if 'LIQUIDO' in payload:
      liquido = float(payload.get('LIQUIDO', 0.0) or 0.0)
    else:
      liquido = bruto - tara
    estavel = str(payload.get('ESTAVEL', payload.get('ESTABILIDADE', 'FALSO'))).upper() in {
        'VERDADEIRO', 'TRUE', '1', 'YES', 'GREEN', 'OK'
    }

    if not estavel:
      raise ValueError('Peso não estabilizado pela balança.')

    return ScaleReading(
        pesoLiquido=liquido,
        pesoBruto=bruto,
        tara=tara,
        estavel=estavel,
        unidade='KG',
    )


class ScaleConnectionHandler:
  """Gerencia leitura por serial/TCP e reconexão automática."""

  def __init__(self, protocol='serial', endpoint='COM1', reconnect_delay_seconds=5):
    self.protocol = protocol
    self.endpoint = endpoint
    self.reconnect_delay_seconds = reconnect_delay_seconds
    self.link = None
    self.connected = False

  def connect(self):
    self.connected = True
    self.link = {'protocol': self.protocol, 'endpoint': self.endpoint}
    return self.link

  def reconnect(self):
    self.connected = False
    self.link = None
    return self.connect()

  def read_line(self, line: str) -> ScaleReading:
    if not self.connected:
      self.connect()
    return ScaleParser.parse(line)


# -----------------------------
# Servico de quebra por resfriamento
# -----------------------------
@dataclass(frozen=True)
class CoolingBreakInput:
  carcaca_id: int
  peso_quente: float
  peso_frio: float
  entrada: datetime
  saida: datetime


@dataclass(frozen=True)
class CoolingBreakOutput:
  quebra_absoluta_kg: float
  percentual_quebra: float
  tempo_resfriamento_horas: float
  alerta_sif: str | None = None


class CoolingBreakException(Exception):
  """Exceção de domínio para regras de processo inválidas."""


class CoolingBreakToleranceAlert(Exception):
  """Alerta de qualidade/SIF quando a quebra estiver fora da faixa aceitável."""


class CoolingBreakService:
  @staticmethod
  def calculate(input_dto: CoolingBreakInput) -> CoolingBreakOutput:
    if input_dto.entrada >= input_dto.saida:
      raise CoolingBreakException('Data/hora de saída deve ser posterior à entrada.')

    hours = (input_dto.saida - input_dto.entrada).total_seconds() / 3600
    if hours < 24:
      raise CoolingBreakException(
          'Tempo de resfriamento insuficiente: o envio para desossa exigiu pelo menos 24 horas.'
      )

    if input_dto.peso_quente <= 0 or input_dto.peso_frio <= 0:
      raise CoolingBreakException('Pesos de entrada devem ser positivos.')

    quebra_absoluta = input_dto.peso_quente - input_dto.peso_frio
    percentual_quebra = (quebra_absoluta / input_dto.peso_quente) * 100

    alerta = None
    if percentual_quebra > 2.5 or percentual_quebra < 1.2:
      alerta = 'ALERTA_SIF_QUALIDADE: quebra fora da tolerância industrial.'

    return CoolingBreakOutput(
        quebra_absoluta_kg=round(quebra_absoluta, 3),
        percentual_quebra=round(percentual_quebra, 3),
        tempo_resfriamento_horas=round(hours, 2),
        alerta_sif=alerta,
    )


# -----------------------------
# Consulta SQL do relatório consolidado de rendimento industrial
# -----------------------------

def consolidated_yield_report_sql() -> str:
  """Retorna uma consulta SQL em CTE com foco em rendimento de carcaça, desossa e quebra."""
  return '''
WITH
recepcao AS (
    SELECT
        id_lote_recepcao,
        codigo_lote,
        numero_gta,
        produtor_origem,
        quantidade_cabecas,
        data_chegada
    FROM lote_recepcao
),

abate AS (
    SELECT
        id_abate,
        id_lote_recepcao,
        sequencial_abate,
        data_hora_abate,
        peso_vivo_kg
    FROM abate
),

carcaca AS (
    SELECT
        c.id_carcaca,
        c.id_abate,
        c.peso_quente_kg,
        c.peso_frio_kg,
        c.data_entrada_camara,
        c.data_saida_camara
    FROM carcaca c
),

desossa_resumo AS (
    SELECT
        odc.id_carcaca,
        SUM(c.peso) AS peso_total_cortes,
        COUNT(*) AS qtde_cortes
    FROM ordem_desossa_carcaca odc
    JOIN ordem_desossa od ON od.id_ordem_desossa = odc.id_ordem_desossa
    JOIN corte_desossa c ON c.id_ordem_desossa_carcaca = odc.id_ordem_desossa_carcaca
    GROUP BY odc.id_carcaca
),

relatorio AS (
    SELECT
        r.codigo_lote AS lote_recepcao,
        r.produtor_origem AS fazenda_produtor,
        r.numero_gta,
        DATE(a.data_hora_abate) AS data_abate,
        ROUND(100.0 * (c.peso_quente_kg / NULLIF(a.peso_vivo_kg, 0)), 2) AS rendimento_carcaca_pct,
        ROUND(100.0 * (COALESCE(d.peso_total_cortes, 0) / NULLIF(c.peso_frio_kg, 0)), 2) AS rendimento_desossa_pct,
        ROUND(100.0 * ((c.peso_frio_kg - COALESCE(d.peso_total_cortes, 0)) / NULLIF(c.peso_frio_kg, 0)), 2) AS quebra_desossa_pct
    FROM recepcao r
    JOIN abate a ON r.id_lote_recepcao = a.id_lote_recepcao
    JOIN carcaca c ON a.id_abate = c.id_abate
    LEFT JOIN desossa_resumo d ON c.id_carcaca = d.id_carcaca
)

SELECT
    lote_recepcao,
    fazenda_produtor,
    numero_gta,
    data_abate,
    rendimento_carcaca_pct,
    rendimento_desossa_pct,
    quebra_desossa_pct
FROM relatorio
ORDER BY data_abate, lote_recepcao, fazenda_produtor;
'''


# -----------------------------
# Web dashboard mínimo para você publicar em domínio próprio
# -----------------------------
if Flask:
  web_app = Flask(__name__)
  estoque_registros = []
  producao_registros = []

  @web_app.get('/api/estoque')
  def api_estoque():
    return jsonify(estoque_registros)

  @web_app.post('/api/estoque')
  def post_estoque():
    payload = request.get_json(silent=True) or request.form.to_dict()
    if not payload:
      return jsonify({'error': 'payload vazio'}), 400

    codigo = payload.get('codigo', '').strip()
    produto = payload.get('produto', '').strip()
    quantidade = float(payload.get('quantidade', 0) or 0)
    setor = payload.get('setor', '').strip()
    if not codigo or not produto or not setor:
      return jsonify({'error': 'codigo, produto e setor são obrigatórios'}), 400

    item = {
        'codigo': codigo,
        'produto': produto,
        'quantidade': quantidade,
        'setor': setor,
        'status': 'em_estoque',
        'created_at': datetime.utcnow().isoformat(),
    }
    estoque_registros.append(item)
    return jsonify({'message': 'estoque registrado', 'item': item}), 201

  @web_app.get('/api/producao')
  def api_producao():
    return jsonify(producao_registros)

  @web_app.post('/api/producao')
  def post_producao():
    payload = request.get_json(silent=True) or request.form.to_dict()
    if not payload:
      return jsonify({'error': 'payload vazio'}), 400

    lote = payload.get('lote', '').strip()
    produto = payload.get('produto', '').strip()
    quantidade = float(payload.get('quantidade', 0) or 0)
    destino = payload.get('destino', '').strip()
    if not lote or not produto or not destino:
      return jsonify({'error': 'lote, produto e destino são obrigatórios'}), 400

    item = {
        'lote': lote,
        'produto': produto,
        'quantidade': quantidade,
        'destino': destino,
        'status': 'armazenado',
        'created_at': datetime.utcnow().isoformat(),
    }
    producao_registros.append(item)
    return jsonify({'message': 'produção registrada', 'item': item}), 201

  @web_app.get('/')
  def index():
    html = '''
    <!doctype html>
    <html lang="pt-BR">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>ERP Frigorífico</title>
      <style>
        :root {
          --bg: #121212;
          --panel: #1e1e1e;
          --panel-soft: #2d2d2d;
          --text: #eaeaea;
          --muted: #a8a8a8;
          --line: #3c3c3c;
          --blue: #42a5f5;
          --blue-2: #0b66b9;
          --green: #34d399;
          --green-soft: #1c6c4a;
          --orange: #f5a524;
          --red: #d84f57;
          --red-soft: #5a2b2b;
          --shadow: rgba(0, 0, 0, 0.45);
        }
        * { box-sizing: border-box; }
        body {
          margin: 0;
          color: var(--text);
          background: var(--bg);
          font-family: Arial, Helvetica, sans-serif;
        }
        .header {
          background: #10141b;
          color: white;
          padding: 22px 36px;
          display: flex;
          align-items: center;
          justify-content: space-between;
          box-shadow: 0 4px 12px var(--shadow);
          border-bottom: 1px solid var(--line);
        }
        .brand { font-size: 28px; font-weight: 700; color: var(--text); }
        .meta { color: var(--muted); font-size: 12px; letter-spacing: 0.08em; }
        .layout {
          display: flex;
          min-height: calc(100vh - 116px);
        }
        .sidebar {
          width: 260px;
          background: #171717;
          padding: 24px 16px;
          border-right: 1px solid var(--line);
        }
        .sidebar h3 {
          margin: 0 0 16px;
          font-size: 14px;
          color: var(--blue);
          letter-spacing: 0.1em;
          text-transform: uppercase;
        }
        .menu-item {
          padding: 12px 14px;
          border-left: 3px solid transparent;
          background: transparent;
          margin-bottom: 8px;
          border-radius: 4px;
          font-weight: 700;
          color: var(--text);
          cursor: pointer;
        }
        .menu-item.active {
          background: var(--blue-2);
          color: white;
          border-left-color: var(--blue);
        }
        .menu-item:hover {
          background: var(--panel-soft);
        }
        .workspace {
          flex: 1;
          padding: 26px;
          background: var(--bg);
        }
        .kpi-grid {
          display: grid;
          grid-template-columns: repeat(4, minmax(150px, 1fr));
          gap: 14px;
          margin-bottom: 20px;
        }
        .kpi-card {
          background: var(--panel);
          border: 1px solid var(--line);
          border-radius: 8px;
          padding: 16px;
          box-shadow: 0 2px 8px var(--shadow);
        }
        .kpi-card .label { font-size: 11px; color: var(--muted); text-transform: uppercase; }
        .kpi-card .value { font-size: 26px; font-weight: 700; margin-top: 10px; color: var(--blue); }
        .panel {
          background: var(--panel);
          border: 1px solid var(--line);
          border-radius: 8px;
          padding: 16px;
          box-shadow: 0 2px 8px var(--shadow);
        }
        .panel-title { font-size: 16px; font-weight: 700; color: var(--text); margin-bottom: 14px; }
        table { width: 100%; border-collapse: collapse; }
        th { text-align: left; background: #171d25; color: var(--blue); padding: 11px; font-size: 11px; }
        td { padding: 11px; border-bottom: 1px solid var(--line); color: var(--text); font-size: 12px; }
        tr:hover td { background: var(--panel-soft); }
        .badge { background: var(--green-soft); color: var(--green); padding: 4px 8px; border-radius: 14px; font-size: 11px; }
        .badge.warn { background: #443000; color: var(--orange); }
        .badge.danger { background: var(--red-soft); color: var(--red); }

        .operacao-grid {
          display: grid;
          grid-template-columns: repeat(2, minmax(280px, 1fr));
          gap: 16px;
          margin-top: 18px;
        }
        .form-card {
          background: var(--panel);
          border: 1px solid var(--line);
          padding: 16px;
          border-radius: 8px;
        }
        .form-card h3 {
          margin: 0 0 15px;
          color: var(--blue);
          font-size: 15px;
        }
        .form-card label {
          display: block;
          color: var(--muted);
          font-size: 11px;
          margin-top: 12px;
        }
        .form-card input, .form-card select {
          width: 100%;
          background: var(--panel-soft);
          color: var(--text);
          border: 1px solid var(--line);
          padding: 9px 10px;
          border-radius: 4px;
          margin-top: 5px;
        }
        .form-card button {
          background: var(--blue);
          color: white;
          border: none;
          padding: 11px 14px;
          border-radius: 4px;
          cursor: pointer;
          margin-top: 14px;
          font-weight: 700;
        }
        .form-card button:hover { background: var(--blue-2); }
        .list-panel { margin-top: 14px; }
        .list-panel table { font-size: 11px; }
      </style>
    </head>
    <body>
      <div class="header">
        <div>
          <div class="brand">FRIGORÍFICO ERP</div>
          <div class="meta">Planta Central • Unidade 01</div>
        </div>
        <div class="meta">Usuário: Operação SIF • 12:45:00 • <span class="badge">ONLINE</span></div>
      </div>

      <div class="layout">
        <aside class="sidebar">
          <h3>Módulos</h3>
          <div class="menu-item active">Recepção</div>
          <div class="menu-item">Abate</div>
          <div class="menu-item">Desossa</div>
          <div class="menu-item">Estoque Frio</div>
          <div class="menu-item">Expedição</div>
          <div class="menu-item">SIF/Qualidade</div>
        </aside>

        <main class="workspace">
          <section class="kpi-grid">
            <div class="kpi-card">
              <div class="label">Recepção</div>
              <div class="value">126.5t</div>
            </div>
            <div class="kpi-card">
              <div class="label">Abate</div>
              <div class="value">842</div>
            </div>
            <div class="kpi-card">
              <div class="label">Desossa</div>
              <div class="value">76.2%</div>
            </div>
            <div class="kpi-card">
              <div class="label">SIF</div>
              <div class="value"><span class="badge">Aprovado</span></div>
            </div>
          </section>

          <section class="panel">
            <div class="panel-title">Controle de produção</div>
            <table>
              <thead>
                <tr>
                  <th>Lote</th>
                  <th>Produtor</th>
                  <th>Abate</th>
                  <th>Carcaça</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                <tr><td>LOTE-001</td><td>Fazenda Nova</td><td>12/09/2026</td><td>128.7 kg</td><td><span class="badge">Liberado</span></td></tr>
                <tr><td>LOTE-002</td><td>Fazenda Azul</td><td>12/09/2026</td><td>131.2 kg</td><td><span class="badge">Aprovado</span></td></tr>
                <tr><td>LOTE-003</td><td>Fazenda Vale</td><td>12/09/2026</td><td>126.9 kg</td><td><span class="badge warn">Em desossa</span></td></tr>
                <tr><td>LOTE-004</td><td>Fazenda Sul</td><td>12/09/2026</td><td>122.9 kg</td><td><span class="badge danger">Condenado</span></td></tr>
              </tbody>
            </table>
          </section>

          <section class="operacao-grid">
            <div class="form-card">
              <h3>Cadastro de Estoque</h3>
              <form id="stockForm">
                <label>Código</label>
                <input type="text" name="codigo" placeholder="EST-0001" required>
                <label>Produto</label>
                <input type="text" name="produto" placeholder="Picanha" required>
                <label>Quantidade (kg)</label>
                <input type="number" name="quantidade" value="0" required>
                <label>Setor</label>
                <select name="setor" required>
                  <option>Armazém Frio</option>
                  <option>Recepção</option>
                  <option>Desossa</option>
                  <option>Expedição</option>
                </select>
                <button type="submit">Registrar estoque</button>
              </form>
              <div class="list-panel">
                <table>
                  <thead><tr><th>Código</th><th>Produto</th><th>Qtde</th><th>Setor</th></tr></thead>
                  <tbody id="estoqueRows"></tbody>
                </table>
              </div>
            </div>

            <div class="form-card">
              <h3>Produção e Armazenagem</h3>
              <form id="producaoForm">
                <label>Lote</label>
                <input type="text" name="lote" placeholder="LOT-0001" required>
                <label>Produto</label>
                <input type="text" name="produto" placeholder="Corte compacto" required>
                <label>Quantidade (kg)</label>
                <input type="number" name="quantidade" value="0" required>
                <label>Destino</label>
                <select name="destino" required>
                  <option>Armazém Frio</option>
                  <option>Expedição</option>
                  <option>Câmara 01</option>
                  <option>Desossa</option>
                </select>
                <button type="submit">Lançar produção</button>
              </form>
              <div class="list-panel">
                <table>
                  <thead><tr><th>Lote</th><th>Produto</th><th>Qtde</th><th>Destino</th></tr></thead>
                  <tbody id="producaoRows"></tbody>
                </table>
              </div>
            </div>
          </section>

          <script>
            async function loadLists() {
              try {
                const estoque = await fetch('/api/estoque');
                const prod = await fetch('/api/producao');
                const estoqueData = await estoque.json();
                const prodData = await prod.json();
                const estoqueRows = document.getElementById('estoqueRows');
                const producaoRows = document.getElementById('producaoRows');
                estoqueRows.innerHTML = '';
                producaoRows.innerHTML = '';

                for (const row of estoqueData) {
                  const tr = document.createElement('tr');
                  tr.innerHTML = '<td>'+row.codigo+'</td><td>'+row.produto+'</td><td>'+row.quantidade+'</td><td>'+row.setor+'</td>';
                  estoqueRows.appendChild(tr);
                }

                for (const row of prodData) {
                  const tr = document.createElement('tr');
                  tr.innerHTML = '<td>'+row.lote+'</td><td>'+row.produto+'</td><td>'+row.quantidade+'</td><td>'+row.destino+'</td>';
                  producaoRows.appendChild(tr);
                }
              } catch (err) {
                console.log(err);
              }
            }

            document.getElementById('stockForm').addEventListener('submit', async function(e) {
              e.preventDefault();
              const data = Object.fromEntries(new FormData(e.target));
              await fetch('/api/estoque', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
              });
              e.target.reset();
              loadLists();
            });

            document.getElementById('producaoForm').addEventListener('submit', async function(e) {
              e.preventDefault();
              const data = Object.fromEntries(new FormData(e.target));
              await fetch('/api/producao', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
              });
              e.target.reset();
              loadLists();
            });

            loadLists();
          </script>
        </main>
      </div>
    </body>
    </html>
    '''
    return html

  @web_app.get('/health')
  def health():
    return jsonify({'status': 'ok', 'service': 'frigorifico-erp', 'timestamp': datetime.utcnow().isoformat()})

  @web_app.get('/api/quality')
  def api_quality():
    return jsonify({'status': 'ok', 'rastreabilidade': True})


Base = declarative_base()


class LoteEntrada(Base):
  __tablename__ = 'lotes_entrada'
  id = Column(Integer, primary_key=True)
  gta_numero = Column(String(50), unique=True, nullable=False)
  produtor = Column(String(100), nullable=False)
  peso_entrada = Column(Float, nullable=False)
  data_entrada = Column(DateTime, default=datetime.utcnow)
  status = Column(String(30), default='No Curral')

  carcacas = relationship(
      'Carcaca', back_populates='lote', cascade='all, delete-orphan'
  )


class Carcaca(Base):
  __tablename__ = 'carcacas'
  id = Column(Integer, primary_key=True)
  lote_id = Column(Integer, ForeignKey('lotes_entrada.id'), nullable=False)
  peso_quente = Column(Float, nullable=False)
  peso_frio = Column(Float, nullable=True)
  quebra_peso = Column(Float, default=0.0)

  lote = relationship('LoteEntrada', back_populates='carcacas')
  cortes = relationship(
      'CorteEstoque', back_populates='carcaca', cascade='all, delete-orphan'
  )


class CorteEstoque(Base):
  __tablename__ = 'cortes_estoque'
  id = Column(Integer, primary_key=True)
  carcaca_id = Column(Integer, ForeignKey('carcacas.id'), nullable=False)
  tipo_corte = Column(String(50), nullable=False)
  peso = Column(Float, nullable=False)
  status_expedicao = Column(String(30), default='Em Estoque')
  validade = Column(DateTime, nullable=False)

  carcaca = relationship('Carcaca', back_populates='cortes')


engine = create_engine('sqlite:///frigorifico_erp.db', echo=False)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)


class FrigorificoERPApp:

  def __init__(self, root):
    self.root = root
    self.root.title('ERP Frigorífico Pro - Gestão Industrial & Rastreabilidade')
    self.root.geometry('1180x760')
    self.root.minsize(900, 640)
    self.root.configure(bg='#f7f7f4')

    self.style = ttk.Style()
    self.style.theme_use('clam')
    self.aplicar_estilo_visual()

    # Cabeçalho corporativo
    self.header = tk.Frame(root, bg='#1f2d35', height=80)
    self.header.pack(fill='x')

    tk.Label(self.header, text='FRIGORÍFICO ERP', bg='#1f2d35', fg='#ffffff',
             font=('Arial', 16, 'bold')).place(x=18, y=16)
    tk.Label(self.header, text='Planta Central • Unidade 01', bg='#1f2d35', fg='#d9d3c8',
             font=('Arial', 10)).place(x=18, y=48)
    tk.Label(self.header, text='Usuário: Operação SIF', bg='#1f2d35', fg='#d9d3c8',
             font=('Arial', 10)).place(x=760, y=18)
    tk.Label(self.header, text='⚙ 12:45:00', bg='#1f2d35', fg='#d9d3c8',
             font=('Arial', 10)).place(x=950, y=18)

    # Sidebar corporativa
    self.sidebar = tk.Frame(root, bg='#ecebe5', width=230)
    self.sidebar.pack(side='left', fill='y')

    self.create_sidebar_items()

    # Área principal
    self.main = tk.Frame(root, bg='#f7f7f4')
    self.main.pack(side='right', fill='both', expand=True)

    # Kpi cards
    self.kpi = tk.Frame(self.main, bg='#f7f7f4')
    self.kpi.pack(fill='x', padx=16, pady=(14, 8))

    self.create_kpi_card('Recepção', '126.5t', '#003a6c', 'GDA/GTA')
    self.create_kpi_card('Abate', '842', '#2d5c76', 'Hoje')
    self.create_kpi_card('Desossa', '76.2%', '#b3661a', 'Rendimento')
    self.create_kpi_card('SIF', 'Aprovado', '#1f6f4b', 'Qualidade')

    self.notebook = ttk.Notebook(self.main)
    self.notebook.pack(fill='both', expand=True, padx=12, pady=8)
    self.notebook.configure(style='ERP.TNotebook')

    self.criar_aba_curral()
    self.criar_aba_camara()
    self.criar_aba_estoque()

  def aplicar_estilo_visual(self):
    # Configuração global de cores e fontes
    self.style.configure('.',
      font=('Arial', 10),
      background='#f7f7f4',
      foreground='#1c2731')

    self.style.configure('ERP.TNotebook', background='#f7f7f4', borderwidth=0)
    self.style.configure('ERP.TNotebook.Tab',
      padding=(16, 10),
      background='#d7d5cd',
      foreground='#1c2731',
      font=('Arial', 10, 'bold'))
    self.style.map('ERP.TNotebook.Tab',
      background=[('selected', '#244b79'), ('active', '#edecea')],
      foreground=[('selected', '#ffffff'), ('active', '#1c2731')])

    self.style.configure('ERP.TFrame', background='#f7f7f4')
    self.style.configure('ERP.TLabel', background='#f7f7f4', foreground='#1c2731')
    self.style.configure('ERP.TButton',
      background='#244b79',
      foreground='#ffffff',
      padding=(14, 8),
      font=('Arial', 10, 'bold'))
    self.style.map('ERP.TButton',
      background=[('active', '#3d6578'), ('!disabled', '#244b79')],
      foreground=[('disabled', '#eef6ee')])

    self.style.configure('Treeview',
      background='#ffffff',
      fieldbackground='#ffffff',
      foreground='#1c2731',
      rowheight=28)
    self.style.map('Treeview',
      background=[('selected', '#d6dce2')],
      foreground=[('selected', '#001b2b')])

  def create_sidebar_items(self):
    sidebar_title = tk.Label(self.sidebar, text='Módulos', bg='#ecebe5',
                              fg='#1f2d35', font=('Arial', 11, 'bold'))
    sidebar_title.pack(anchor='w', padx=16, pady=(18, 10))

    modules = [
      ('Recepção', 'GDA/GTA'),
      ('Abate', 'Controle'),
      ('Desossa', 'Cortes'),
      ('Estoque Frio', 'Câmara'),
      ('Expedição', 'Faturamento'),
      ('SIF/Qualidade', 'Auditoria'),
    ]

    for idx, (name, desc) in enumerate(modules, start=1):
      frame = tk.Frame(self.sidebar, bg='#ecebe5')
      frame.pack(fill='x', padx=12, pady=3)
      tk.Label(frame, text=name, bg='#ecebe5', fg='#1c2731', font=('Arial', 10, 'bold')).pack(anchor='w')
      tk.Label(frame, text=desc, bg='#ecebe5', fg='#65716a', font=('Arial', 9)).pack(anchor='w')

  def create_kpi_card(self, title, value, color, label):
    card = tk.Frame(self.kpi, bg='#ffffff', width=180, height=70, highlightbackground='#d9d3c8', highlightthickness=1)
    card.pack(side='left', fill='y', padx=8)
    card.pack_propagate(False)
    tk.Label(card, text=title, bg='#ffffff', fg='#65716a', font=('Arial', 10)).place(x=10, y=8)
    tk.Label(card, text=value, bg='#ffffff', fg=color, font=('Arial', 16, 'bold')).place(x=10, y=28)
    tk.Label(card, text=label, bg='#ffffff', fg='#65716a', font=('Arial', 9)).place(x=10, y=52)

  def criar_aba_curral(self):
    frame = ttk.Frame(self.notebook, style='ERP.TFrame')
    self.notebook.add(frame, text='Curral & Abate')

    ttk.Label(
        frame,
        text='Cadastro de lote e abate',
        font=('Arial', 14, 'bold'),
        style='ERP.TLabel',
    ).grid(row=0, column=0, columnspan=2, pady=(12, 16), sticky='w')

    ttk.Label(frame, text='Número da GTA:', style='ERP.TLabel').grid(
        row=1, column=0, sticky='w', padx=(20, 8), pady=8
    )
    self.ent_gta = ttk.Entry(frame, width=34)
    self.ent_gta.grid(row=1, column=1, padx=8, pady=8)

    ttk.Label(frame, text='Produtor / Fazenda:', style='ERP.TLabel').grid(
        row=2, column=0, sticky='w', padx=(20, 8), pady=8
    )
    self.ent_produtor = ttk.Entry(frame, width=34)
    self.ent_produtor.grid(row=2, column=1, padx=8, pady=8)

    ttk.Label(frame, text='Peso total do lote (kg):', style='ERP.TLabel').grid(
        row=3, column=0, sticky='w', padx=(20, 8), pady=8
    )
    self.ent_peso_lote = ttk.Entry(frame, width=34)
    self.ent_peso_lote.grid(row=3, column=1, padx=8, pady=8)

    ttk.Label(frame, text='Peso quente da carcaça (kg):', style='ERP.TLabel').grid(
        row=4, column=0, sticky='w', padx=(20, 8), pady=8
    )
    self.ent_peso_quente = ttk.Entry(frame, width=34)
    self.ent_peso_quente.grid(row=4, column=1, padx=8, pady=8)

    btn_salvar = ttk.Button(
        frame, text='Registrar lote e abate', command=self.salvar_abate
    )
    btn_salvar.grid(row=5, column=0, columnspan=2, pady=(20, 10), sticky='ew')

  def criar_aba_camara(self):
    frame = ttk.Frame(self.notebook, style='ERP.TFrame')
    self.notebook.add(frame, text='Câmara Fria & Desossa')

    ttk.Label(
        frame,
        text='Controle de resfriamento e cortes',
        font=('Arial', 14, 'bold'),
        style='ERP.TLabel',
    ).grid(row=0, column=0, columnspan=2, pady=(12, 16), sticky='w')

    ttk.Label(frame, text='ID da carcaça:', style='ERP.TLabel').grid(
        row=1, column=0, sticky='w', padx=(20, 8), pady=8
    )
    self.ent_carcaca_id = ttk.Entry(frame, width=34)
    self.ent_carcaca_id.grid(row=1, column=1, padx=8, pady=8)

    ttk.Label(frame, text='Peso frio pós-resfriamento (kg):', style='ERP.TLabel').grid(
        row=2, column=0, sticky='w', padx=(20, 8), pady=8
    )
    self.ent_peso_frio = ttk.Entry(frame, width=34)
    self.ent_peso_frio.grid(row=2, column=1, padx=8, pady=8)

    ttk.Label(frame, text='Tipo de corte produzido:', style='ERP.TLabel').grid(
        row=3, column=0, sticky='w', padx=(20, 8), pady=8
    )
    self.ent_tipo_corte = ttk.Entry(frame, width=34)
    self.ent_tipo_corte.insert(0, 'Picanha')
    self.ent_tipo_corte.grid(row=3, column=1, padx=8, pady=8)

    ttk.Label(frame, text='Peso do corte (kg):', style='ERP.TLabel').grid(
        row=4, column=0, sticky='w', padx=(20, 8), pady=8
    )
    self.ent_peso_corte = ttk.Entry(frame, width=34)
    self.ent_peso_corte.grid(row=4, column=1, padx=8, pady=8)

    btn_processar = ttk.Button(
        frame,
        text='Processar resfriamento',
        command=self.processar_camara,
    )
    btn_processar.grid(row=5, column=0, columnspan=2, pady=(20, 10), sticky='ew')

  def criar_aba_estoque(self):
    frame = ttk.Frame(self.notebook, style='ERP.TFrame')
    self.notebook.add(frame, text='Estoque')

    ttk.Label(
        frame,
        text='Consulta de estoque por corte',
        font=('Arial', 14, 'bold'),
        style='ERP.TLabel',
    ).grid(row=0, column=0, pady=(12, 12), sticky='w')

    self.tree = ttk.Treeview(
        frame,
        columns=('ID', 'Corte', 'Peso', 'Validade', 'Status'),
        show='headings',
        height=18,
    )
    self.tree.heading('ID', text='ID')
    self.tree.heading('Corte', text='Tipo de corte')
    self.tree.heading('Peso', text='Peso (kg)')
    self.tree.heading('Validade', text='Validade')
    self.tree.heading('Status', text='Status')
    self.tree.column('ID', width=60, anchor='center')
    self.tree.column('Corte', width=180)
    self.tree.column('Peso', width=100, anchor='center')
    self.tree.column('Validade', width=120)
    self.tree.column('Status', width=120)
    self.tree.grid(row=1, column=0, padx=(16, 16), pady=5, sticky='nsew')

    btn_atualizar = ttk.Button(
        frame, text='Atualizar estoque', command=self.carregar_estoque
    )
    btn_atualizar.grid(row=2, column=0, pady=(12, 16), sticky='w')

    frame.grid_columnconfigure(0, weight=1)
    frame.grid_rowconfigure(1, weight=1)
    self.carregar_estoque()

  def salvar_abate(self):
    session = Session()
    try:
      gta = self.ent_gta.get()
      produtor = self.ent_produtor.get()
      peso_lote = float(self.ent_peso_lote.get())
      peso_quente = float(self.ent_peso_quente.get())

      novo_lote = LoteEntrada(
          gta_numero=gta, produtor=produtor, peso_entrada=peso_lote
      )
      session.add(novo_lote)
      session.flush()

      nova_carcaca = Carcaca(lote_id=novo_lote.id, peso_quente=peso_quente)
      session.add(nova_carcaca)
      session.commit()

      messagebox.showinfo(
          'Sucesso',
          f'Lote registrado com sucesso! ID da Carcaça gerada: {nova_carcaca.id}',
      )
    except Exception as e:
      session.rollback()
      messagebox.showerror('Erro', f'Falha ao registrar transação: {e}')
    finally:
      session.close()

  def processar_camara(self):
    session = Session()
    try:
      c_id = int(self.ent_carcaca_id.get())
      peso_frio = float(self.ent_peso_frio.get())
      tipo_corte = self.ent_tipo_corte.get()
      peso_corte = float(self.ent_peso_corte.get())

      carcaca = session.query(Carcaca).filter_by(id=c_id).first()
      if not carcaca:
        messagebox.showerror('Erro', 'Carcaça não encontrada.')
        return

      carcaca.peso_frio = peso_frio
      carcaca.quebra_peso = round(
          ((carcaca.peso_quente - peso_frio) / carcaca.peso_quente) * 100, 2
      )

      novo_corte = CorteEstoque(
          carcaca_id=carcaca.id,
          tipo_corte=tipo_corte,
          peso=peso_corte,
          validade=datetime.utcnow() + timedelta(days=30),
      )
      session.add(novo_corte)
      session.commit()

      messagebox.showinfo(
          'Sucesso',
          f'Resfriamento processado! Quebra de peso registrada:'
          f' {carcaca.quebra_peso}%',
      )
      self.carregar_estoque()
    except Exception as e:
      session.rollback()
      messagebox.showerror('Erro', f'Erro no processamento: {e}')
    finally:
      session.close()

  def carregar_estoque(self):
    for item in self.tree.get_children():
      self.tree.delete(item)

    session = Session()
    cortes = session.query(CorteEstoque).all()
    for c in cortes:
      self.tree.insert(
          '',
          'end',
          values=(
              c.id,
              c.tipo_corte,
              c.peso,
              c.validade.strftime('%d/%m/%Y'),
              c.status_expedicao,
          ),
      )
    session.close()


if __name__ == '__main__':
  if os.getenv('ERP_WEB', '0') == '1' and Flask is not None:
    web_app.run(host='0.0.0.0', port=5000, debug=False)
  else:
    root = tk.Tk()
    app = FrigorificoERPApp(root)
    root.mainloop()