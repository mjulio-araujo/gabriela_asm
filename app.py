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
    numero_nota: str = Field(default="N/D", description="Número da Nota Fiscal, Fatura, Recibo ou Holerite")
    data_emissao: str = Field(default="N/D", description="Data de emissão do documento principal")
    cnpj_prestador: str = Field(default="N/D", description="CNPJ ou CPF do Prestador, Fornecedor ou Funcionário")
    razao_social_prestador: str = Field(default="N/D", description="Nome ou Razão Social do Prestador, Fornecedor ou Funcionário")
    cnpj_tomador: str = Field(default="N/D", description="CNPJ do Tomador de Serviços")
    razao_social_tomador: str = Field(default="N/D", description="Nome ou Razão Social do Tomador")
    municipio_tomador: str = Field(default="N/D", description="Município do Tomador")
    descricao_servico: str = Field(default="N/D", description="Descrição do serviço, produto, rubrica ou despesa")
    valor_total: float = Field(default=0.0, description="Valor Total do documento em formato decimal")
    codigo_servico: str = Field(default="N/D", description="Código do Serviço prestado (se houver)")
    data_pagamento: str = Field(default="N/D", description="Data registrada no comprovante de pagamento/transferência")
    banco_pagamento: str = Field(default="N/D", description="Instituição financeira onde o pagamento foi processado")
    autenticacao_pagamento: str = Field(default="N/D", description="Código de autenticação bancária ou ID da transação")

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
    Extraia os dados financeiros do bloco de texto fornecido.
    Retorne EXCLUSIVAMENTE um objeto JSON contendo EXATAMENTE as seguintes chaves:
    "numero_nota", "data_emissao", "cnpj_prestador", "razao_social_prestador", "cnpj_tomador", "razao_social_tomador", "municipio_tomador", "descricao_servico", "valor_total", "codigo_servico", "data_pagamento", "banco_pagamento", "autenticacao_pagamento".
    Regras de mapeamento:
    - Se o documento não possuir um campo específico, preencha com a string "N/D".
    - A chave "valor_total" deve ser estritamente numérica (float). Se não houver valor, retorne 0.0. Não use a string "N/D" neste campo.
    - cnpj_prestador e razao_social_prestador devem conter os dados de quem recebeu o valor.
    - Localize os dados de pagamento na seção de comprovante bancário ou transferência.
    Não adicione formatação markdown.
    """
    prompt_completo = f"{instrucao}\n\nTEXTO DO DOCUMENTO:\n{bloco_texto}"
    
    try:
        response = client.chat.completions.create(
            model="gpt-5.6-luna",
            messages=[{"role": "user", "content": prompt_completo}],
            response_format={"type": "json_object"}
        )
        return response.choices[0].message.content
    except Exception as e:
        st.error(f"Falha na API da OpenAI: {e}")
        return None

arquivos_pdf = st.file_uploader("Selecione os arquivos PDF", type=["pdf"], accept_multiple_files=True)

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
            
            if "Lançamento 0" in texto_completo:
                blocos = re.split(r'(?=\bLançamento \d{5}\b)', texto_completo)
                # Filtra a folha de rosto e fatias inválidas. Apenas blocos que começam com o termo Lançamento são processados.
                blocos_validos = [b for b in blocos if re.search(r'^\s*Lançamento \d{5}', b)]
            else:
                blocos_validos = [texto_completo]
            
            total_blocos = len(blocos_validos)
            
            for i, bloco in enumerate(blocos_validos, 1):
                status_texto.text(f"Processando arquivo {idx_arquivo + 1}/{total_arquivos} | Lançamento {i}/{total_blocos}")
                
                if bloco.strip():
                    resposta_bruta = processar_bloco_llm(bloco)
                    if resposta_bruta:
                        try:
                            resposta_limpa = resposta_bruta.replace("```json", "").replace("```", "").strip()
                            dados_dict = json.loads(resposta_limpa)
                            dados_validados = TransacaoSchema(**dados_dict).model_dump()
                            
                            linha = [
                                dados_validados.get('numero_nota', 'N/D'),
                                dados_validados.get('data_emissao', 'N/D'),
                                dados_validados.get('cnpj_prestador', 'N/D'),
                                dados_validados.get('razao_social_prestador', 'N/D'),
                                dados_validados.get('cnpj_tomador', 'N/D'),
                                dados_validados.get('razao_social_tomador', 'N/D'),
                                dados_validados.get('municipio_tomador', 'N/D'),
                                dados_validados.get('descricao_servico', 'N/D'),
                                dados_validados.get('valor_total', 0.0),
                                dados_validados.get('codigo_servico', 'N/D'),
                                dados_validados.get('data_pagamento', 'N/D'),
                                dados_validados.get('banco_pagamento', 'N/D'),
                                dados_validados.get('autenticacao_pagamento', 'N/D'),
                                arquivo.name 
                            ]
                            linhas_para_inserir.append(linha)
                        except Exception as e:
                            st.error(f"Erro de parser Pydantic no Lançamento {i} do arquivo {arquivo.name}: {e}")
            
            barra_progresso.progress((idx_arquivo + 1) / total_arquivos)

        if linhas_para_inserir:
            try:
                status_texto.text("Inicializando I/O com Google Sheets...")
                aba.append_rows(linhas_para_inserir, value_input_option='USER_ENTERED')
                
                url_planilha = f"https://docs.google.com/spreadsheets/d/{PLANILHA_ID}/edit"
                st.success(f"**Operação concluída.** {len(linhas_para_inserir)} registros inseridos remotamente.")
                st.markdown(f"[🔗 Clique aqui para visualizar a planilha no Google Sheets]({url_planilha})")
                
                status_texto.empty()
            except Exception as e:
                st.error(f"Falha na gravação da planilha: {e}")
