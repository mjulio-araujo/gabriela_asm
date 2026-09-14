import streamlit as st
import fitz
import json
import re
import pandas as pd
import gspread
from openai import OpenAI
from pydantic import BaseModel, Field

st.set_page_config(page_title="Extrator Híbrido NFS-e e Prestação de Contas", layout="centered")
st.title("Extração Semântica e Financeira")

class TransacaoSchema(BaseModel):
    numero_nota: str = Field(description="Número da Nota Fiscal, Fatura, Recibo ou Holerite")
    data_emissao: str = Field(description="Data de emissão do documento principal")
    cnpj_prestador: str = Field(description="CNPJ ou CPF do Prestador, Fornecedor ou Funcionário")
    razao_social_prestador: str = Field(description="Nome ou Razão Social do Prestador, Fornecedor ou Funcionário")
    cnpj_tomador: str = Field(description="CNPJ do Tomador de Serviços")
    razao_social_tomador: str = Field(description="Nome ou Razão Social do Tomador")
    municipio_tomador: str = Field(description="Município do Tomador")
    descricao_servico: str = Field(description="Descrição do serviço, produto, rubrica ou despesa")
    valor_total: float = Field(description="Valor Total do documento em formato decimal")
    codigo_servico: str = Field(description="Código do Serviço prestado (se houver)")
    data_pagamento: str = Field(description="Data registrada no comprovante de pagamento/transferência")
    banco_pagamento: str = Field(description="Instituição financeira onde o pagamento foi processado")
    autenticacao_pagamento: str = Field(description="Código de autenticação bancária ou ID da transação")

try:
    API_KEY = st.secrets["OPENAI_API_KEY"]
    PLANILHA_ID = st.secrets["PLANILHA_ID"]
    credenciais_gcp = dict(st.secrets["gcp_service_account"])
    
    client = OpenAI(api_key=API_KEY)
    gc = gspread.service_account_from_dict(credenciais_gcp)
    planilha = gc.open_by_key(PLANILHA_ID)
    aba = planilha.sheet1
except Exception as e:
    st.error(f"Erro de infraestrutura: Credenciais não localizadas. {e}")
    st.stop()

def extrair_texto_pdf(arquivo_bytes):
    texto = ""
    try:
        doc = fitz.open(stream=arquivo_bytes, filetype="pdf")
        for pagina in doc:
            texto += pagina.get_text()
        doc.close()
    except Exception as e:
        st.error(f"Falha técnica na extração de texto: {e}")
    return texto

def processar_bloco_llm(bloco_texto):
    instrucao = """
    Extraia os dados financeiros e de pagamento do bloco de texto fornecido. O bloco contém o documento de cobrança (NF, Fatura, Holerite) e seu respectivo comprovante bancário.
    Retorne EXCLUSIVAMENTE um objeto JSON válido.
    Regras de mapeamento:
    - Se o documento não possuir um campo específico (ex: codigo_servico em um Holerite), preencha com "N/D".
    - cnpj_prestador e razao_social_prestador devem conter os dados de quem recebeu o valor (fornecedor, funcionário, concessionária).
    - Localize os dados de pagamento (data_pagamento, banco_pagamento, autenticacao_pagamento) na seção de comprovante bancário, recibo PIX ou transferência.
    Não adicione formatação markdown (```json).
    """
    prompt_completo = f"{instrucao}\n\nTEXTO DO DOCUMENTO:\n{bloco_texto}"
    
    try:
        response = client.chat.completions.create(
            model="gpt-5.6-luna",
            messages=[{"role": "user", "content": prompt_completo}],
            response_format={"type": "json_object"},
            temperature=0.0
        )
        return response.choices[0].message.content
    except Exception as e:
        st.error(f"Falha na API da OpenAI: {e}")
        return None

arquivos_pdf = st.file_uploader("Selecione os arquivos PDF (NF-e individual ou Prestação de Contas em lote)", type=["pdf"], accept_multiple_files=True)

if st.button("Processar Documentos e Enviar para Planilha"):
    if not arquivos_pdf:
        st.warning("Forneça no mínimo um arquivo PDF.")
    else:
        linhas_para_inserir = []
        barra_progresso = st.progress(0)
        status_texto = st.empty()
        
        total_arquivos = len(arquivos_pdf)
        
        for idx_arquivo, arquivo in enumerate(arquivos_pdf):
            texto_completo = extrair_texto_pdf(arquivo.read())
            
            # Estratégia de Roteamento Dinâmico: Verifica se é um arquivo consolidado (Relatório) ou NF individual
            if "Lançamento 0" in texto_completo:
                blocos = re.split(r'(?=\bLançamento \d{5}\b)', texto_completo)
                blocos_validos = [b for b in blocos if len(b.strip()) > 100]
            else:
                blocos_validos = [texto_completo]
            
            total_blocos = len(blocos_validos)
            
            for i, bloco in enumerate(blocos_validos, 1):
                status_texto.text(f"Processando arquivo {idx_arquivo + 1}/{total_arquivos} | Bloco {i}/{total_blocos}")
                
                if bloco.strip():
                    resposta_bruta = processar_bloco_llm(bloco)
                    if resposta_bruta:
                        try:
                            resposta_limpa = resposta_bruta.replace("```json", "").replace("```", "").strip()
                            dados_dict = json.loads(resposta_limpa)
                            dados_validados = TransacaoSchema(**dados_dict).model_dump()
                            
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
                                dados_validados['data_pagamento'],
                                dados_validados['banco_pagamento'],
                                dados_validados['autenticacao_pagamento'],
                                arquivo.name 
                            ]
                            linhas_para_inserir.append(linha)
                        except Exception as e:
                            st.error(f"Erro de parser/validação Pydantic. Bloco {i} do arquivo {arquivo.name}: {e}")
            
            barra_progresso.progress((idx_arquivo + 1) / total_arquivos)

        if linhas_para_inserir:
            try:
                status_texto.text("Inicializando I/O com Google Sheets...")
                aba.append_rows(linhas_para_inserir, value_input_option='USER_ENTERED')
                
                url_planilha = f"[https://docs.google.com/spreadsheets/d/](https://docs.google.com/spreadsheets/d/){PLANILHA_ID}/edit"
                st.success(f"**Operação concluída.** {len(linhas_para_inserir)} registros inseridos remotamente.")
                st.markdown(f"[🔗 Clique aqui para visualizar a planilha no Google Sheets]({url_planilha})")
                
                status_texto.empty()
            except Exception as e:
                st.error(f"Falha na gravação da planilha: {e}")
