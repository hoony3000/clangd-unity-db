# clangd-unity-db

`main.c → project.h → a.c / b.c`처럼 `.c` 파일을 include하는 C 프로젝트에서,
각 `.c`를 Neovim/clangd로 열 때 앞서 선언한 함수·타입·매크로를 인식하도록
**clangd 전용** `compile_commands.json`을 만듭니다. 원본 소스는 수정하지 않으며
진단을 끄는 옵션도 추가하지 않습니다.

## 필요한 환경

- Linux, Python 3.6 이상 (외부 Python 패키지 없음)
- 실제 빌드에 사용하는 GCC, Makefile 기록 시 GNU make
- Clang VFS overlay를 지원하는 clangd

CentOS 7.4에서는 이미 설치한 Python 3.10과 GCC를 사용하세요.
CentOS의 Python 2.7로 실행하면 안 됩니다. 생성된 파일에는 절대 경로가 있으므로
**실제 소스와 GCC가 있는 원격 서버에서 실행**해야 합니다.
GCC 4.8 및 해당 서버의 clangd 바이너리 조합은 별도 현장 검증이 필요합니다.

## 가장 권장하는 사용법: Makefile의 실제 옵션 기록

프로젝트 루트에서 다음 명령을 실행합니다. 스크립트 경로는 설치 위치로 바꾸세요.

```bash
python3 /path/to/gen_clangd_unity_db.py \
  --capture build-commands.json -- make -B

python3 /path/to/gen_clangd_unity_db.py src/main.c \
  --build-db build-commands.json
```

**`src/main.c`는 실제 GCC가 컴파일하는 파일**입니다. 중간에 있는 `project.h`를
지정하면 그 헤더 앞에서 `main.c`가 설정한 문맥은 얻을 수 없습니다.
기록된 컴파일 대상이 하나뿐이라면 두 번째 명령의 `src/main.c`는 생략할 수 있습니다.
여러 대상이나 같은 대상의 여러 설정이 있으면 하나의 빌드 명령만 선택해야 합니다.

첫 번째 명령은 실제 빌드를 실행합니다. `-B`는 재빌드로 컴파일 명령을 기록하기 위한
옵션입니다. 기존 빌드에 필요한 target과 변수도 그대로 전달할 수 있습니다.

```bash
python3 /path/to/gen_clangd_unity_db.py --cc /usr/bin/gcc \
  --capture build-commands.json -- make -B my_target MODE=debug -j4
```

Makefile은 `$(CC)`를 사용해야 합니다. 이 도구는 이번 make 호출에만 기록용 CC를
전달하며, 원래 GCC 명령을 실행하고 성공한 C 컴파일을 기록합니다. 셸 설정이나
Makefile은 변경하지 않습니다. `gcc`가 레시피에 하드코딩되어 있거나 `override CC`
를 쓰면 기록할 수 없으므로 아래 수동 옵션 방식 또는 기존 compilation database를
사용하세요. `--cc`에는 직접 GCC 실행 파일을 지정하는 것을 권장합니다.

`build-commands.json`은 **실제 빌드용 입력 기록**이고, `compile_commands.json`은
**clangd용 출력**입니다. 서로 다른 파일로 유지하세요. Bear는 필요 없습니다.
환경 변수로 전달되는 빌드 설정은 DB에 저장되지 않으므로 같은 빌드 환경에서 실행하세요.

## 빌드 옵션을 직접 지정하는 경우

```bash
python3 /path/to/gen_clangd_unity_db.py src/main.c \
  --cflags='-std=gnu99 -Iinclude -I/path/to/vendor/include -DFEATURE_A=1'
```

`--flag=-Iinclude`, `--flag=-DFEATURE_A=1`도 반복해서 사용할 수 있습니다.
`--build-db`를 사용하면서 추가 옵션을 지정하면 기록된 옵션 뒤에 추가됩니다.
원래 빌드가 헤더 자체를 translation unit으로 사용하는 경우에만 그 헤더를 지정하세요.

## Neovim에서 적용

프로젝트 루트에 출력된 `compile_commands.json`을 clangd가 찾도록 한 뒤 재시작합니다.
사용 중인 LSP 설정에서 제공한다면 다음 명령을 사용하세요.

```vim
:LspRestart
```

서버 터미널에서도 검사할 수 있습니다.

```bash
clangd --check=/absolute/project/src/b.c \
  --compile-commands-dir=/absolute/project
```

로그에 compilation database에서 읽은 명령과 `-ivfsoverlay`, `-include`가
나오는지 확인하세요. 원본 `.clangd`에 `-W*` 제거 등 진단 억제 설정을 넣을 필요는 없습니다.
기존 설정에서 compile flags를 덮어쓰고 있다면 생성된 명령과 충돌하는지 확인하세요.

## 동작 방식

1. 실제 루트 소스와 빌드 옵션으로 **GCC `-E -dI`**를 실행합니다.
   `.c` include를 제거하거나 다른 지시문으로 대체하지 않습니다.
2. GCC의 파일 진입·복귀 표식을 따라 활성 include 경로를 찾습니다.
   중첩 헤더, 매크로 include, 여러 줄 include, 앞선 `.c`가 변경한 매크로와
   `#if/#elif/#else`가 실제 전처리에 반영됩니다.
3. 대상 `.c`에 도달하기 전까지의 상위 소스 내용을 복사하고 열린 조건부를 닫습니다.
4. 대상별 VFS overlay로 **상위 파일의 원래 경로**를 해당 복사본에 연결합니다.
   clangd가 원래 루트 파일을 forced include하면 대상 직전의 문맥까지만 읽게 됩니다.
   원래 경로를 유지하므로 상대 include와 기존 헤더의 `#pragma once`도 유지됩니다.
5. 대상 `.c` 자체는 원본으로 분석합니다. 루트 소스도 정상 DB 항목으로 포함합니다.

생성된 컴파일 명령에는 Clang 전용 `-ivfsoverlay`가 들어갑니다.
이 DB를 GCC 빌드 도구에 입력하지 마세요. 실제 빌드는 기존 Makefile을 사용합니다.

관련 문서: [GCC 전처리 옵션](https://gcc.gnu.org/onlinedocs/gcc/Preprocessor-Options.html),
[LLVM VFS overlay](https://llvm.org/doxygen/classllvm_1_1vfs_1_1RedirectingFileSystem.html).

## 갱신과 오류 처리

- 소스의 include 순서, 상위 파일의 선언, 매크로, 빌드 옵션이 바뀌면 다시 생성하세요.
  빌드 옵션이 같다면 기존 `build-commands.json`을 재사용할 수 있습니다.
- 전처리가 실패하면 오류를 출력하며 기존 DB를 교체하지 않습니다.
- 생성물은 `.clangd-unity/run-*/`에 저장됩니다. 이전 DB가 참조하던 파일이
  실행 도중 덮어써지지 않도록 실행별 디렉터리를 사용합니다.
- 오래된 run 디렉터리는 clangd를 종료한 뒤 정리할 수 있습니다.
  현재 DB가 가리키는 디렉터리는 지우면 안 됩니다.
- 각 run의 `manifest.json`에서 대상별 include 경로를 확인할 수 있습니다.

## 지원 범위와 제한

- 한 번의 생성은 하나의 루트 소스와 하나의 빌드 설정을 처리합니다.
  비활성 분기의 `.c`는 포함하지 않습니다. 과거의 `--all`과 `--keep-probe`는 제거했습니다.
- 같은 `.c`를 여러 번 포함하거나 같은 상위 파일을 반복 진입한 문맥은 모호하므로
  오류로 중단합니다. command-line forced header 내부의 `.c`도 현재 지원하지 않습니다.
- 파일 최상위에서 완전한 선언/함수 단위의 `.c`를 포함하는 C unity build가 대상입니다.
  함수 본문 중간에서 코드 조각을 include하는 구조는 지원하지 않습니다.
- 명시적 `#line`으로 위치를 바꾸는 상위 파일은 지원하지 않습니다.
  소스는 UTF-8을 사용해야 하며 trigraph 지시문은 지원하지 않습니다.
- GCC와 Clang 고유 매크로에 따라 분기가 달라지는 코드는 별도 조정이 필요합니다.
  `__BASE_FILE__`, `__INCLUDE_LEVEL__`처럼 translation unit 문맥에 의존하는 코드는
  실제 빌드와 차이가 날 수 있습니다.
- 일반 GCC 명령의 `arguments` 또는 셸 인용된 단순 `command` DB를 받습니다.
  `cd ... && gcc ...`, 환경변수 할당, ccache 등 셸/런처 체인은 DB에서 제거해야 합니다.
  응답 파일(`@flags.rsp`)은 지원합니다. 입력 소스는 명령당 하나여야 합니다.
- 빌드의 `-o`, dependency 출력 옵션은 제거합니다. 일부 특수 전처리/PCH/출력 옵션은
  조용히 오해석하지 않고 거부합니다. GCC 전용 분석 불가 옵션은 clangd 설정에서
  개별 조정해야 할 수 있습니다.

## 테스트

```bash
python3 -m unittest discover -s tests -v
```

GCC로 실제 include 추적과 Makefile 기록을 검증합니다. clang/clangd가 있으면
생성된 명령으로 타입·선언 참조와 `#pragma once`를 검사하고, 의도적으로 넣은
실제 오류가 여전히 진단되는지도 확인합니다. GitHub Actions는 두 도구를 설치해 실행합니다.

## License

MIT
