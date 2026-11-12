# Indoor Trainer

Treinos de ciclismo com rolo Bluetooth FTMS, gravação FIT e barra sobre o Stremio.

## Instalação e arranque

Requer Python 3.10+, `bleak` e `PyQt5`:

```sh
pip install bleak PyQt5
python ergo.py
```

A janela inicial permite escolher treino FIT, potência constante ou treino livre,
definir a duração opcional, procurar dispositivos Bluetooth e selecionar o rolo
e a cinta. Também aceita um nome parcial ou endereço Bluetooth escrito à mão.
O modo de simulação permite experimentar sem hardware.

Abre o Stremio e coloca o filme em ecrã inteiro. A barra ocupa toda a largura
do topo do ecrã principal; não altera a proporção do vídeo. A primeira linha
mostra potência média dos últimos 3 segundos, objetivo, pulsação, cadência,
velocidade e distância. A segunda mostra tempos, bloco atual, próximo bloco
e controlos. O fundo é transparente e a potência fica laranja perto do objetivo (±5% + 5 W), amarela
até ±15% + 10 W e vermelha fora desse intervalo. Sem objetivo mantém o laranja.

Os botões −/+ ajustam o objetivo em 5 W (também funcionam as teclas quando a
barra tem foco). Em treino livre, o primeiro ajuste ativa um objetivo de potência.
Num FIT, o ajuste aplica-se ao bloco atual; o seguinte usa o objetivo do ficheiro.
Esc, botão direito e «Menu» abrem as opções continuar, pausar ou terminar e guardar.
Abrir o menu não pausa: usa «Pausar treino». A pausa manual retira o alvo de
potência, suspende o tempo e a gravação; continuar repõe o objetivo.
Nos treinos FIT aparece também um perfil em escadinhas, com a largura de cada
bloco proporcional à duração e a altura à potência. Uma linha branca indica
o ponto atual; os blocos livres aparecem com altura mínima.
Sem pedalada recente, o tempo e a gravação também ficam suspensos automaticamente.

Potência e duração são opcionais. Sem duração a sessão termina no menu.
Os treinos com amostras são guardados em `~/rides`; `--out` permite outra pasta.
Uma falha de ligação ao rolo termina a sessão e guarda as amostras existentes.

## Linha de comandos

```sh
python ergo.py --gui --ftp 282 --trainer "Think" --hr "Decathlon"
python ergo.py treino.fit --overlay --yes
python ergo.py --power 180 --duration 45 --overlay --yes
python ergo.py --duration 30 --overlay --sim --yes
python ergo.py --scan
python ergo.py --help
```

O mpv e o overlay HTTP continuam disponíveis através de `--film` e `--web`.
A interface desktop destina-se a sobrepor leitores externos como o Stremio.

## Organização

- [ergo.py](ergo.py): argumentos e execução da sessão.
- [desktop.py](desktop.py): preparação, pesquisa e barra desktop.
- [devices.py](devices.py): Bluetooth e simulação.
- [workouts.py](workouts.py): interpretação dos blocos de treino.
- [fit_io.py](fit_io.py): leitura e gravação FIT.
- [overlays.py](overlays.py): apresentação mpv e HTTP.

Testes: `python -m unittest -v`.
