# DefectDojo Open Source: Compose kurulum ve bakım

Greenbone ve DefectDojo ayrı VM'lerde çalışır. Bu repo upstream'i fork etmez;
resmî **3.4.202** Compose tanımını ve bütün imaj digest'lerini sabitler. Kubernetes,
Pro CLI/connector, Watchtower veya zorunlu SaaS kullanılmaz.

## Ön gereksinimler

- Ayrılmış Linux amd64 VM, Python **3.10+**, Docker Engine **25+**, Docker Compose
  **2.24+** plugin. `make` sadece kolaylık komutları içindir.
- Docker daemon erişimi olan operatör hesabı. Docker grubu hostta root yetkisi
  kadar güçlüdür; bu hesabı kısıtlayın. Paketleri/grupları kendiniz yapılandırın;
  CLI eksikleri kontrol eder, habersiz paket kurmaz.
- Docker Hub/GitHub HTTPS çıkışı; kurum CA/proxy'sini Docker daemon ve Python
  trust store'unda önceden yapılandırın.
- Hedef **4.000 varlık**, aylık tarama, Info dahil yaklaşık **500.000 birikmiş
  bulgu**; her ay 500.000 yeni kayıt hedeflenmez. Greenbone ayrı VM'dedir;
  DefectDojo/PostgreSQL/web/Celery/Valkey aynı Compose VM'sinde kalır.
- Planlanan üretim başlangıcı: **16 vCPU, 64 GB RAM, 80 GB sistem + genişletilebilir
  750 GB SSD veri diski**, VM dışında yedek. Bu kaynaklar kapasite garantisi
  değildir; **500.000 bulguyla yük testi yapılmadı**.
- Pilot config ve davranışı korunur (worker iki prefork süreç). İsteğe bağlı
  [tek VM üretim örneği](config.production.example.json), RAM bütçesi, gerçek veri
  diski kurulumu ve ölçüm/kabul adımları: [üretim rehberi](docs/production.md).
  Worker CPU sayısına göre otomatik artırılmaz; üretim de iki worker ile başlar.

## Sıfırdan kurulum ve ilk giriş

Repo kökünde, kurulumu işletecek hesapla ilk komut:

```sh
./dojo init
```

Bu komut yalnızca `.runtime/` oluşturur; servis başlatmaz. `config.json` ve
`secrets.json` 0600, dizin 0700'dür. `.runtime/` ve `backups/` git dışında kalır.
AES/Django anahtarlarını yeniden üretmeyin; eski credential'lar okunamaz olabilir.

1. `.runtime/config.json` dosyasını inceleyin. Lab varsayılanı
   `http://localhost:8080`, yalnızca **127.0.0.1:8080** dinlenir. DB, Valkey,
   uWSGI host portu açılmaz; `allowed_hosts` wildcard kabul etmez.
2. `./dojo doctor` ile bağımlılık/Compose ve gerçek Docker/veri/yedek filesystem
   alanlarını kontrol edin. Üretimde önce [veri diski ve VM dışı mount](docs/production.md)
   hazırlığını tamamlayın; alan eşikleri kapasite planının yerine geçmez.
3. `./dojo install` ile sabit imajları çekip initializer/migration ve sağlık
   doğrulamasını tamamlayın. İlk başlangıç uzun sürebilir.
4. Uzak lab erişimi için kendi bilgisayarınızda
   `ssh -L 8080:127.0.0.1:8080 operator@defectdojo-vm` çalıştırın;
   `http://localhost:8080/login` adresine gidin. Kullanıcı **admin**.
5. İlk parolayı yalnızca kendi yönetici terminalinizde okuyun:

```sh
python3 -c 'import json; print(json.load(open(".runtime/secrets.json"))["DD_ADMIN_PASSWORD"])'
```

Parolayı ilk girişte değiştirin, günlük işler için sınırlı yetkili kullanıcı
oluşturun. Secret dosyasındaki admin değeri ilk giriş parolası olarak kalır;
initializer mevcut kullanıcının parolasını sıfırlamaz.

## HTTPS ve erişim

Kurum erişimi için VM üzerindeki mevcut nginx benzeri reverse proxy'de TLS
sonlandırın: [nginx örneği](docs/nginx-https.conf.example). DNS, kurum CA'sından
sertifika, proxy kurulumu ve firewall değişikliği operatöre aittir.
`.runtime/config.json` içindeki şu alanları değiştirin (dosyanın kalanını koruyun):

```json
{
  "site_url": "https://dojo.example.internal",
  "allowed_hosts": ["dojo.example.internal", "localhost", "127.0.0.1"],
  "https": true
}
```

`./dojo start` kontrollü yeniden başlatma yapar. Proxy yalnızca hosttaki loopback
backend'e gitmeli, `Host` bilgisini taşımalı ve `X-Forwarded-Proto` değerini kendisi
`https` yazmalıdır. CLI secure session/CSRF cookie, trusted origin ve proxy HTTPS
ayarlarını yapar. Dış IP'ye doğrudan HTTP bind seçeneği yoktur. Firewall'da yalnızca
kurum/VPN'den 443 ve yönetim SSH erişimi verin; DB/Valkey'i dışarı açmayın.

Host reverse proxy konfigürasyonu ve TLS private key'lerini kurum host/PKI yedek
politikanıza dahil edin; CLI host dosyalarını okumaz. Sertifika geri yükleme/yenileme
provasını uygulama restore provasıyla birlikte yapın. Uygulamanın kendi bütün
secret ve anahtarları uygulama yedeğine dahildir.

## Bakım komutları

| Komut | İşlem |
|---|---|
| `./dojo init` | Yerel ayarlar ve rastgele secret; mevcut state'i ezmez |
| `./dojo install` | Yeni kurulumu resmî sabit imajlarla kurar |
| `./dojo start` | Durdurulan sistemi açar; açıksa kontrollü yeniden başlatır |
| `./dojo stop` | Import/işleri boşaltıp servisleri durdurur; veriyi silmez |
| `./dojo status` | Kalıcı bakım durumu ve gerçek konteyner durumları |
| `./dojo doctor` | Bağımlılık, Compose, DB/Valkey/Django/worker/scheduler/HTTP |
| `./dojo backup` | Tutarlı, doğrulanmış yedek; ardından sistemi açar |
| `./dojo check-update` | Yeni kararlı sürümü loglar; yükseltme veya pull yapmaz |
| `./dojo upgrade X.Y.Z` | Yalnızca policy'deki doğrulanmış tam sürüm geçişi |
| `./dojo restore /path/backup --confirm 'RESTORE defectdojo'` | Açık onayla aktif kurulumu yedekteki veriye geçirir |
| `./dojo start --resume` | Başarısız bakım/upgrade sonrası aktif hedefi açıkça tekrar dener |

Global seçenekler alt komuttan önce: `./dojo --dry-run upgrade 3.4.202` veya
`./dojo --state /private/absolute/path status`. Dry-run sadece niyeti gösterir;
dosya, kilit, secret, Docker, ağ veya timer değişikliği yapmaz. Sağlık/uyumluluk
doğrulaması sayılmaz. Her gerçek komut aynı state'in dosya kilidini alır. Aynı
kurulumu ikinci state dizininden veya bakım sırasında doğrudan Compose ile yönetmeyin.
Kurulumdan sonra `project` adını değiştirmeyin; ayrı proje için yeni state'e restore edin.

Exit code: **0** başarı, **1** işletim/kilit/konfigürasyon/uyumluluk hatası,
**2** CLI kullanım hatası, **3** status için hazır olmayan kurulum, **130** kesinti.
Loglar UTC ve İngilizcedir. Bilinen secret'lar/URL parolaları maskelenir. Doğrudan
Docker config/inspect ve upstream logları özel veri içerebilir; paylaşmadan temizleyin.

## Backup ve retention

Backup bakım penceresi ister: nginx aktif HTTP/import isteklerini tamamlayarak
kapanır; scheduler/web üreticileri durur. uWSGI stats socket üzerinden devam eden
senkron istekler de kontrol edilir; istemci timeout olsa bile import bitmeden web
süreci durdurulmaz. Worker active/reserved/scheduled işleri,
broker priority list'leri ve unacknowledged teslimler boşalmadan yedek alınmaz.
Yoğun worker inspect'e cevap veremeyebilir; süre sonuna kadar beklenir. İşler
purge edilmez, importlar SIGKILL ile kesilmez. `drain_timeout` (900 sn varsayılan)
dolarsa işlem hata verir, erişim kapalı kalır. Üretim örneği drain için 4 saat
önerir; gerçek iş süresiyle ayarlayın. Dump/media için ayrı `backup_timeout`,
restore için `restore_timeout` kullanılır. Toplam bakım kesintisi drain başlangıcından
yeniden hazır olana kadar `maintenance.json` ve loga yazılır; uzak kopya ve yeniden
aktivasyon da dahildir. Hata sonrası açık pencere retry boyunca sürer.

Her yedek şunları taşır:

- Tutarlı PostgreSQL `pg_dump --format=custom --no-owner` ve gzip media arşivi.
- Konfigürasyon, admin ilk parolası, Django secret, credential AES anahtarı,
  JIRA webhook secret, volume kimlikleri ve kullanılan Compose dosyası.
- Sürümün dağıtım/lisans dosyaları, kaynak commit'i, imaj digest'leri ve yerel
  image ID/RepoDigests; bütün dosyaların SHA-256'larını içeren manifest.

Kalıcı named volume'ler `<project>_defectdojo_postgres_<nesil>`,
`<project>_defectdojo_media_<nesil>` ve `<project>_defectdojo_redis_<nesil>` adlarını
kullanır. İlk nesil `initial`; restore yeni nesil oluşturur. Nesil bilgisi
`.runtime/volumes.json`, kesin volume adları özel Compose dosyasındadır.

Valkey kuyrukları boşaltıldığı için cache/kuyruk restore edilmez; boş broker
oluşturulur. Boşaltma doğrulanamazsa backup reddedilir. Yedek önce `.partial-*`
dizinine yazılır; checksum, media içerik/yolları ve `pg_restore --list` kontrolü
sonra yayınlanır. Dizinler 0700, dosyalar 0600'dür.

Yedekler **şifrelenmemiştir**, hassas veri taşır. Backup diski/ayrı hedef için
kurum disk şifrelemesi ve erişim kontrolünü uygulayın. Checksum kazara bozulmayı
tespit eder; kaynağı doğrulayan imza değildir. `backup_dir` mutlak veya repo köküne
göre yoldur; varsayılan `backups/`. `retention_days: 14` başarılı yeni yedekten
sonra eski günlük yedekleri temizler. En yeni günlük yedek ve **bütün upgrade
yedekleri** korunur. Upgrade ve `.partial-*` temizliği operatöre aittir.

`backup_copy_dir` önceden oluşturulmuş 0700 izinli ayrı hedefin mutlak yolu olabilir
(`backup_copy_mount` ile beklenen gerçek mount da belirtilir):
şifreli ayrı disk veya güvenli biçimde bağlanmış uzak filesystem. Kopya checksum ile
doğrulanır; başarısızsa upgrade/retention devam etmez. Mount'u siz yönetirsiniz;
zorunlu SSH hesabı, SaaS veya bildirim servisi yoktur. Uzak retention bağımsızdır.
Aynı VM diski tek başına felaket kurtarma yedeği değildir; düzenli ayrı restore
provası yapın. `backup_timeout`, `restore_timeout` ve disk eşiklerini gerçek hacme göre ölçün.
`backup_copy_mount` zorunlu beklenen mount yoludur; mount yokken yerel dizine
başarılı kopya raporlanmaz. Üretim profili VM dışı uzak filesystem ister.
Ayrıntılar ve eski yedeklerde alan tahmini: [üretim rehberi](docs/production.md).

## Günlük backup ve periyodik yeni sürüm kontrolü

`systemd/` altındaki dört dosya şablondur, otomatik kurulmaz. Önce manuel kurulum
ve backup başarılı olmalı. Hedef VM'de siz:

1. Repo'yu kalıcı bir konuma taşıyın; şablonlardaki
   `/opt/defectdojo-update-manager`, `User`, `Group`, `SupplementaryGroups`
   alanlarını mevcut operatör hesabınıza uyarlayın. Aynı hesap state/backup'a erişmeli.
2. Günlük 02:00 backup, pazartesi 09:00 kontrol saatlerini bakım pencerenize göre
   düzenleyin. Host yerel saati kullanılır; `Persistent=true` kaçırılan işi sonradan
   çalıştırabilir. Backup şablonunda `TimeoutStartSec=infinity` ve `TimeoutStopSec=infinity`
   kullanılır; uzun bakım systemd tarafından kesilmez. CLI aşama sürelerini yönetin.
   Kendi veri/yedek mount yollarınızla `RequiresMountsFor=` ekleyin.
3. Dört dosyayı `/etc/systemd/system/` içine kopyalayın; ardından:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now defectdojo-backup.timer defectdojo-check-update.timer
systemctl list-timers 'defectdojo-*'
journalctl -u defectdojo-backup.service -u defectdojo-check-update.service
```

Otomatik uygulama yükseltmesi yoktur. Bildirim için `notification_hook` isteğe bağlı
mutlak executable/argüman dizisidir: `["/usr/local/bin/dojo-notify"]`. CLI sonuna
yalnızca `backup:success` benzeri sonuç ekler, shell kullanmaz ve 15 sn bekler.
Hook hatası asıl işlemin exit code'unu değiştirmez. Kilit çakışması journal'a hata düşer.

## Upgrade ve geri dönüş

Önce `check-update`, resmî release ve **ara sürümlerin** geçiş notlarını okuyun.
Yeni tag uyumluluk onayı değildir. Paket/policy hazırlığı: [docs/upgrades.md](docs/upgrades.md).
Doğrulanmış yollar ve test sınırları: [docs/validation.md](docs/validation.md).

```sh
./dojo status
./dojo doctor
./dojo --dry-run upgrade X.Y.Z
./dojo upgrade X.Y.Z
```

Akış: preflight → hedef pull → bakım/yazıları durdurma → iş boşaltma → doğrulanmış
upgrade yedeği ve ayrı kopya → sürüm/dosya setini değiştirme → resmî initializer
exit 0 → `migrate --check`, DB/Valkey/Django/worker/scheduler/HTTP → erişimi açma.
Pull/backup hatası yeni migration başlatmaz. Migration/sağlık hatası erişimi kapalı
bırakır; veri otomatik eski yedekle değiştirilmez. PostgreSQL/Valkey imajı değişen
geçişler bu otomasyon tarafından ayrı veri taşıma prosedürü olmadan reddedilir.

Eski imajı açmak migration'ı geri almaz. Şema değiştiyse **upgrade öncesi** doğru
sürüm, DB, media ve secret setini birlikte geri getirin:

```sh
./dojo status
./dojo restore /secure/backups/20261023T020000000000Z-upgrade --confirm 'RESTORE defectdojo'
./dojo doctor
```

Onaydaki isim `config.json` içindeki `project` olmalıdır. Restore checksum, yollar,
manifest/sürüm, konfigürasyon, media ve PostgreSQL arşivini değişiklikten önce
kontrol eder. Eski volume'leri silmeden **yeni volume nesline** yükler; aktif kurulum
bu veriye geçirilir. Yedekten sonraki değişiklikler yeni aktif sistemde bulunmaz.
Hedef VM'nin project/port/hostname/HTTPS/backup/hook, kaynak/upload/disk ve timeout
ayarları korunur; uygulama sürümü
ve secret'ları yedekten gelir. Eski metadata `.runtime/history/` içinde saklanır.

Eski volume'ler disk tüketir; restore doğrulanıp retention kararı verildikten sonra
açık isimleriyle manuel temizleyin; `docker volume prune` kullanmayın. Yeni VM'de
önce `init`, ortam ayarları ve `doctor`, ardından restore yeterlidir. Backup kendi
sürüm paketini taşır; imaj katmanlarını taşımaz. Registry erişimi kaybına karşı
imaj arşivi/kurum registry kopyası ve host TLS yedeği ayrıca planlanmalıdır.
`config.json` içindeki kurulu `version` alanını elle değiştirmeyin.

## Sorun giderme

| Sorun | Toparlanma |
|---|---|
| Docker izni/paketi | `docker info`, `docker compose version`; bağımlılık/izinleri siz düzeltin |
| Lock | Diğer komutu bekleyin; lock dosyasını silmeyin veya ikinci Compose açmayın |
| Pull | DNS, rate limit, daemon CA/proxy/registry erişimi; pull hatası mevcut servisi durdurmaz |
| Drain timeout | Import/worker'ı bekleyin; aynı sürümde `start --resume` ile devam edin |
| Backup/kopya hatası | Disk/izin/mount'u düzeltin; `start --resume` sonrası yeniden backup; upgrade başlamamıştır |
| Initializer çalışıyor | Öldürmeyin; `start --resume` aynı initializer'ı bekler; restore/stop/backup onu kesmez |
| Migration exit nonzero | Özel logları inceleyin; aynı hedef `start --resume` veya önceki yedeğe onaylı restore |
| Sağlık hatası | `doctor`, DB/Valkey/worker/HTTP logları; `failed` durumda sıradan start engellenir |
| Restore yarıda | `restoring` start'ı engeller; aynı backup ve açık onay ile restore'ı tekrar edin |
| 413/büyük import | [Üç katmanlı upload/timeout ayarlarını](docs/production.md) eşleştirin; `start` ardından `doctor`; eksik raporu kapatıcı reimport etmeyin |

Özel loglar yalnızca yönetici terminalinde:

```sh
docker compose --project-name defectdojo --env-file /dev/null -f .runtime/compose.json logs --tail 100 initializer
docker compose --project-name defectdojo --env-file /dev/null -f .runtime/compose.json logs --tail 100 celeryworker
```

## Greenbone: ilk manuel import ve REST API yolu

[Resmî OpenVAS rehberi](https://docs.defectdojo.com/supported_tools/parsers/file/openvas/)
yeni işler için **OpenVAS Parser v2** önerir. Sabit sürüm XML ve CSV kabul eder.
Parser sürümünü koruyun; parser/hash değişikliği deduplication'ı değiştirebilir.

1. Greenbone'da tamamlanmış ve aynı sabit host kapsamının tamamını içeren taramayı
   seçin; native XML/CSV dışa aktarın, PDF kullanmayın. Filtre, eksik rapor ve
   erişilemeyen/başarısız hostları kontrol edin.
2. Organization (Product Type) → sabit kapsam için Asset (Product) → Engagement
   oluşturun. API bu nesnelerde `product_type` ve `product` adlarını korur. Engagement
   içinde **Import Scan**, **OpenVAS Parser v2**, rapor ve tarih seçin.
   İlk pilotta **Close Old Findings kapalı** olsun.
3. Oluşan **Test ID**'yi saklayın. Aynı kapsamın sonraki raporlarını **aynı Test'e
   Reimport** edin; her ay yeni Test açmayın. Farklı kapsamlar ayrı Test'tir.
   Host/endpoint/bulgu sayılarını Greenbone raporuyla karşılaştırın.
4. Başarısız, eksik, kesilmiş veya kapsamı değişmiş tarama bulgunun düzeldiğini
   kanıtlamaz. Otomatik kapanış için kullanmayın; reimportu durdurun veya
   `close_old_findings=false` seçin. Yalnızca tam, başarılı aynı kapsamı doğruladıktan
   sonra kapanışı bilinçli açın.

İkinci aşama: Greenbone VM raporun başarı/bütünlük/kapsamını kontrol ettikten sonra
HTTPS REST API'ye push eder; Pro connector gerekmez. UI'dan API token alın;
token sahibi kullanıcıya yalnızca ilgili Asset/Product için gereken yetkiyi verin.
Çalışan instance'ın `/api/v2/oa3/swagger-ui/` şemasını
kontrol edin. İlk `/api/v2/import-scan/` isteğinde `engagement`, `scan_type`, `file`;
sonraki `/api/v2/reimport-scan/` isteklerinde sabit `test` kullanılır.

Token'ı process argümanı/history'ye koymadan 0600 izinli `/secure/dojo-curl.conf`
dosyasında `header = "Authorization: Token ..."` tutun. Tek rapor örneği:

```sh
curl --fail-with-body --silent --show-error --max-time 3600 \
  --config /secure/dojo-curl.conf \
  --form 'test=123' \
  --form 'scan_type=OpenVAS Parser v2' \
  --form 'close_old_findings=false' \
  --form 'file=@/secure/reports/completed-scope.xml' \
  https://dojo.example.internal/api/v2/reimport-scan/
```

Kurum CA'sını istemciye ekleyin, TLS doğrulamasını kapatmayın. HTTP/JSON sonucunu
ve Test/bulgu sayılarını doğrulayın; timeout sonrası sonucu kontrol etmeden tekrar
göndermeyin. İlk pilotta bütün importları sırayla çalıştırın. Aynı Test'e eşzamanlı
reimport yapmayın. Bu repo tam tarama
orkestrasyon platformu kurmaz.

## Doğrulama ve lisans

```sh
make test static validate
make integration
```

İlk kontroller Docker daemon istemez; Compose CLI gerekir. `integration` gerçek
imajlarla rastgele proje/port/volume'de örnek verili install → backup → veri/media
değiştirme → restore → upgrade provası yapıp yalnız kendi kaynaklarını temizler.
Gerçek sonuçlar ve doğrulanmayan yollar: [docs/validation.md](docs/validation.md).

Kökteki mevcut [LICENSE](LICENSE) korunur. Vendored DefectDojo dağıtım dosyaları
kendi [BSD lisansına](releases/3.4.202/LICENSE.md) tabidir; kaynak/commit atıfları
her sürümün `release.json` dosyasındadır.
