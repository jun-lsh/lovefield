from Crypto.Util.number import getRandomRange
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
import os

BANNER = """
   __                                                       __                          __   
  / /    ____         _______  ___        ____ ___.__.     |  | __  _______  ______.__. \ \  
 / /    / ___\       / ___\  \/  /       / ___<   |  |     |  |/ / / ___\  \/  <   |  |  \ \ 
 \ \   / /_/  >     / /_/  >    <       / /_/  >___  |     |    < / /_/  >    < \___  |  / / 
  \_\  \___  / /\   \___  /__/\_ \ /\   \___  // ____| /\  |__|_ \\___  /__/\_ \/ ____| /_/  
      /_____/  )/  /_____/      \/ )/  /_____/ \/      )/       \/_____/      \/\/           
"""

def hash(num):
    while True:
        g = getRandomRange(1, p - 1)
        if pow(g, (p-1) // 2, p) == p - 1:
            break
    return (g, pow(g, num, p))

def elGamalEncrypt(publicKey, message):
    y = getRandomRange(1, publicKey[2] - 1)
    c1 = pow(publicKey[0], y, publicKey[2])
    c2 = message * pow(publicKey[1], y, publicKey[2]) % publicKey[2]
    return (c1, c2)

def elGamalDecrypt(privateKey, ciphertext):
    s = pow(ciphertext[0], privateKey[0], privateKey[1])
    m = ciphertext[1] * pow(s, -1, privateKey[1]) % privateKey[1]
    return m

p = 406625734936259380148542405676477203607 
# p = 2 * q + 1 for some prime q. This will prevent any pesky small subgroup attacks!
g = 2
x = getRandomRange(1, p-1)
h = pow(g, x, p)

publicKey = (g, h, p)
privateKey = (x, p)

print(BANNER)
print(f'{publicKey = }')

flag = b'NCO26{REDACTED}'
aesKey = os.urandom(16) # 128 bits
aesCipher = AES.new(aesKey, AES.MODE_ECB)
encFlag = aesCipher.encrypt(pad(flag, 16)).hex()
print(f'{encFlag = }')

for i in range(137):
    print("===============================\n0 - Get key\n1 - Decrypt El Gamal Ciphertext\n===============================")
    choice = int(input(">> "))
    if not choice:
        encryptedKey = elGamalEncrypt(publicKey, int.from_bytes(aesKey, "big"))
        print(encryptedKey)
    else:
        c1 = int(input("Enter c1\n>> ")) % p
        c2 = int(input("Enter c2\n>> ")) % p
        elGamalPlaintext = elGamalDecrypt(privateKey, (c1, c2))
        print("Hashed Plaintext:", hash(elGamalPlaintext)[1]) # oops. i left a [1] in there.