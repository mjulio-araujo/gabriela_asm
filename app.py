import streamlit as st
import fitz
import json
import pandas as pd
import gspread
from openai import OpenAI
from pydantic import BaseModel, Field

# Configuração de Interface Web
st.set_page_config(page_title="Extrator NFS-e", layout="centered")
st.title("Extração Semântica de Notas Fiscais")

class NotaFiscalSchema(BaseModel):
    numero_nota: str = Field(description="Número da Nota Fiscal")
    data_emissao: str = Field(description="Data de emissão (ex: 10/09/2026)")
    cnpj_prestador: str = Field(description="CNPJ do Prestador de Serviços")
    razao_social_prestador: str = Field(description="Nome ou Razão Social do Prestador")
    cnpj_tomador: str = Field(description="CNPJ do Tomador de Serviços")
    razao_social_tomador: str = Field(description="Nome ou Razão Social do Tomador")
    municipio_tomador: str = Field(description="Município do Tomador de Serviços")
    descricao_servico: str = Field(description="Texto contido em Discriminação de Serviços")
    valor_total: float = Field(description="Valor Total do Serviço (ex: 11359.73)")
    codigo_servico: str = Field(description="Código do Serviço prestado")

# Inicialização de dependências com bloqueio de falhas via Secrets
try:
    API_KEY = st.secrets["OPENAI_API_KEY"]
    PLANILHA_ID = st.secrets["PLANILHA_ID"]
    credenciais_gcp = dict(st.secrets["gcp_service_account"])
    
    client = OpenAI(api_key=API_KEY)
    gc = gspread.service_account_from_dict(credenciais_gcp)
    planilha = gc.open_by_key(PLANILHA_ID)
    aba = planilha.sheet1
except Exception as e:
    st.error(f"Erro de infraestrutura: Credenciais não localizadas no cofre da nuvem. {e}")
    st.stop()

def extrair_texto_pdf(arquivo_bytes):
    texto = ""
    try:
        doc = fitz.open(stream=arquivo_bytes, filetype="pdf")
        for pagina in doc:
            texto += pagina.get_text()
        doc.close()
    except Exception as e:
        st.error(f"Falha técnica na leitura de bytes do PDF: {e}")
    return texto

def processar_texto_llm(texto):
    instrucao = """
    Retorne EXCLUSIVAMENTE um objeto JSON. Não adicione texto ou formatação.
    Chaves: numero_nota (string), data_emissao (string), cnpj_prestador (string), razao_social_prestador (string), cnpj_tomador (string), razao_social_tomador (string), municipio_tomador (string), descricao_servico (string), valor_total (float), codigo_servico (string).
    """
    prompt_completo = f"{instrucao}\n\nTEXTO DA NOTA FISCAL:\n{texto}"
    
    try:
        response = client.chat.completions.create(
            model="gpt-5.6-luna",
            messages=[{"role": "user", "content": prompt_completo}],
            response_format={"type": "json_object"},
            reasoning_effort="high"
        )
        return response.choices[0].message.content
    except Exception as e:
        st.error(f"Erro na comunicação com a API de LLM: {e}")
        return None

arquivos_pdf = st.file_uploader("Selecione os arquivos PDF", type=["pdf"], accept_multiple_files=True)

if st.button("Processar Notas e Enviar para Planilha"):
    if not arquivos_pdf:
        st.warning("Forneça no mínimo um arquivo PDF.")
    else:
        linhas_para_inserir = []
        total = len(arquivos_pdf)
        barra_progresso = st.progress(0)
        status_texto = st.empty()
        
        for i, arquivo in enumerate(arquivos_pdf, 1):
            status_texto.text(f"Executando pipeline [{i}/{total}]: {arquivo.name}")
            texto = extrair_texto_pdf(arquivo.read())
            
            if texto.strip():
                resposta_bruta = processar_texto_llm(texto)
                if resposta_bruta:
                    try:
                        resposta_limpa = resposta_bruta.replace("```json", "").replace("```", "").strip()
                        dados_dict = json.loads(resposta_limpa)
                        dados_validados = NotaFiscalSchema(**dados_dict).model_dump()
                        linha = [
                            dados_validados['numero_nota'],
                            dados_validados['data_emissao'],
                            dados_validados['cnpj_prestador'],
                            dados_validados['razao_social_prestador'],
                            dados_validados['cnpj_tomador'],
                            dados_validados['razao_social_tomador'],
                            dados_validados['municipio_tomador'],
                            dados_validados['descricao_servico'],
                            dados_validados['valor_total'],
                            dados_validados['codigo_servico'],
                            arquivo.name 
                        ]
                        linhas_para_inserir.append(linha)
                    except Exception as e:
                        st.error(f"Erro de validação Pydantic no arquivo {arquivo.name}: {e}")
            
            barra_progresso.progress(i / total)

        if linhas_para_inserir:
            try:
                status_texto.text("Inicializando comunicação com Google Sheets...")
                aba.append_rows(linhas_para_inserir, value_input_option='USER_ENTERED')
                st.success(f"Operação concluída. {len(linhas_para_inserir)} registros inseridos remotamente.")
                status_texto.empty()
            except Exception as e:
                st.error(f"Falha na gravação I/O da planilha: {e}")
