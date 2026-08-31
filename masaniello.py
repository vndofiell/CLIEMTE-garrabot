import pandas as pd
import logging
import traceback

logger = logging.getLogger(__name__)


class Masaniello:
    def __init__(self, quantidade, vitorias, pay, banca, modo='Normal'):
        """
        Inicia o Gerenciamento Masaniello.
        :param quantidade: Total de operações permitidas no ciclo (ex: 10)
        :param vitorias: Alvo de vitórias necessárias (ex: 5)
        :param pay: Payout da corretora em formato decimal (ex: 1.85 para 85%)
        :param banca: Capital alocado para este ciclo específico
        """
        self.quantidade = quantidade
        self.vitorias = vitorias
        self.pay = float(pay)
        self.banca = float(banca)
        self.banca_final = float(banca)
        self.modo = modo
        self.primeira_entrada = True
        self.ultima_entrada = 0
        self.qnt_wins = 0
        self.qnt_loss = 0

        # Cria a tabela de probabilidades usando Pandas
        self.tabela = pd.DataFrame(
            index=range(0, self.quantidade + 2),
            columns=range(0, self.vitorias + 2)
        )
        self._calcula_tabela()

    def _calcula_tabela(self):
        """Constrói a matriz de coeficientes do Masaniello"""
        tabela = self.tabela
        for linha in range(0, self.quantidade + 2):
            for coluna in range(0, self.vitorias + 2):
                if linha == 0:
                    tabela.loc[linha, coluna] = 0
                elif coluna == 0:
                    tabela.loc[linha, coluna] = 1
                elif coluna > self.vitorias - 1 or linha > self.quantidade - 1:
                    tabela.loc[linha, coluna] = 'NaN'
                else:
                    try:
                        valor_celula_baixo = tabela.loc[linha + 1, coluna]
                        valor_celula_lado = tabela.loc[linha, coluna + 1]
                    except:
                        valor_celula_baixo = 'NaN'
                        valor_celula_lado = 'NaN'

                    if linha == self.quantidade - 1:
                        valor_celula_baixo = '1' if coluna == 0 else 'NaN'
                    if coluna == self.vitorias - 1:
                        valor_celula_lado = '1' if linha == 0 else 'NaN'

                    if valor_celula_baixo == 'NaN' and valor_celula_lado == 'NaN':
                        tabela.loc[linha, coluna] = 'NaN'
                    else:
                        # Fórmula central do Masaniello
                        tabela.loc[linha, coluna] = (
                            float(self.pay) * float(valor_celula_baixo) * float(valor_celula_lado) /
                            (float(valor_celula_baixo) + (float(self.pay - 1)) * float(valor_celula_lado))
                        )
        self.tabela = tabela

    def calcula_primeira(self):
        """Calcula o valor da primeira aposta do ciclo"""
        try:
            linha2x2 = float(self.tabela.loc[1, 1])
            linha1x2 = float(self.tabela.loc[1, 0])
            self.ultima_entrada = round(
                (1 - self.pay * linha2x2 / (linha1x2 + (self.pay - 1) * (linha2x2))) * self.banca, 2
            )
            return abs(self.ultima_entrada)
        except:
            return 0.35  # Valor mínimo de segurança

    def calcula_entrada(self, result=None):
        """
        Calcula a próxima entrada baseada no resultado anterior.
        :param result: 'win', 'loss' ou None (para a primeira)
        """
        try:
            if self.primeira_entrada:
                self.ultima_entrada = self.calcula_primeira()
                self.primeira_entrada = False
                return self.ultima_entrada

            # Atualiza saldo e contadores baseados no resultado recebido
            if result == 'win':
                self.banca_final += self.ultima_entrada * (self.pay - 1)
                self.qnt_wins += 1
            elif result == 'loss':
                self.banca_final -= self.ultima_entrada
                self.qnt_loss += 1

            # Verifica se o ciclo acabou (atingiu meta de wins ou estourou limite de loss)
            if self.qnt_wins >= self.vitorias or self.qnt_loss + self.qnt_wins >= self.quantidade:
                self.ultima_entrada = 0
            else:
                try:
                    # Busca os coeficientes na tabela para a posição atual do placar
                    linha2x2 = float(self.tabela.loc[self.qnt_loss + self.qnt_wins + 1, self.qnt_wins + 1])
                    linha1x2 = float(self.tabela.loc[self.qnt_loss + self.qnt_wins + 1, self.qnt_wins])

                    self.ultima_entrada = round(
                        (1 - self.pay * linha2x2 / (linha1x2 + (self.pay - 1) * (linha2x2))) * self.banca_final, 2
                    )
                except:
                    # Caso de erro no índice, aposta o restante da banca alocada
                    self.ultima_entrada = self.banca_final

            return abs(self.ultima_entrada)

        except Exception as e:
            logger.error(f"Erro no cálculo do Masaniello: {e}\n{traceback.format_exc()}")
            return -1
