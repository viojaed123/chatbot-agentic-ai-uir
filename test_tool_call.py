
import json
import urllib.request
import urllib.error


OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "hermes3:8b"


# Definisi tool yang diberikan kepada Hermes
tools = [
    {
        "type": "function",
        "function": {
            "name": "cari_uir",
            "description": (
                "Mencari informasi publik mengenai Universitas Islam Riau "
                "dari situs resmi UIR. Gunakan fungsi ini ketika pengguna "
                "meminta informasi tentang UIR yang belum tersedia "
                "dalam percakapan."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Kata kunci informasi yang dicari."
                    }
                },
                "required": ["query"]
            }
        }
    }
]


messages = [
    {
        "role": "system",
        "content": (
            "Kamu adalah asisten informasi Universitas Islam Riau. "
            "Jika pengguna meminta informasi faktual tentang UIR "
            "yang belum tersedia dalam percakapan, gunakan tool cari_uir. "
            "Jangan mengarang informasi. Jawab dalam bahasa Indonesia."
        )
    },
    {
        "role": "user",
        "content": "Cari informasi tentang sejarah Universitas Islam Riau."
    }
]


payload = {
    "model": MODEL,
    "messages": messages,
    "tools": tools,
    "stream": False,
    "options": {
        "temperature": 0
    }
}


def main():
    print("=" * 55)
    print("PENGUJIAN TOOL CALLING HERMES 3 8B")
    print("=" * 55)

    request = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            result = json.loads(response.read().decode("utf-8"))

        message = result.get("message", {})
        tool_calls = message.get("tool_calls", [])

        print("\nModel:", result.get("model"))
        print("\nJawaban model:", message.get("content", ""))

        if tool_calls:
            print("\nSTATUS: BERHASIL")
            print("Hermes berhasil memilih tool.\n")

            for index, call in enumerate(tool_calls, start=1):
                function = call.get("function", {})

                print(f"Tool ke-{index}:")
                print("Nama fungsi:", function.get("name"))
                print(
                    "Argumen:",
                    json.dumps(
                        function.get("arguments", {}),
                        indent=4,
                        ensure_ascii=False
                    )
                )
        else:
            print("\nSTATUS: BELUM BERHASIL")
            print("Hermes belum mengembalikan pemanggilan tool.")

    except urllib.error.URLError as error:
        print("\nGAGAL menghubungi Ollama.")
        print("Pastikan Ollama sedang berjalan.")
        print("Detail:", error)

    except Exception as error:
        print("\nTerjadi kesalahan:", error)


if __name__ == "__main__":
    main()
