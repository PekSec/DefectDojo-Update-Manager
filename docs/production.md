# Tek VM üretim başlangıcı ve pilot kabulü

Hedef: Greenbone ayrı VM; DefectDojo, PostgreSQL 18, nginx/uWSGI, Celery worker/beat
ve Valkey aynı Compose VM'sinde. 4.000 varlık, aylık tarama, Info dahil yaklaşık
500.000 **birikmiş** bulgu; her ay 500.000 yeni kayıt oluşturma hedefi yoktur.
16 vCPU, 64 GB RAM, 80 GB sistem diski, genişletilebilir 750 GB SSD veri diski
başlangıç tahminidir. Yedek hedefi VM dışındadır. **500.000 bulguyla yük testi,
büyük gerçek yedek/restore ve kurum raporuyla import/reimport yapılmadı.**

## Profili seçme

`config.example.json` pilot davranışını korur: worker 2 prefork, web upstream'deki
4 süreç × 4 thread, PostgreSQL varsayılanları; container bellek limiti eklenmez.
CPU sayısı paralelliği artırmaz. `config.production.example.json` isteğe bağlı,
tam bir başlangıç örneğidir; `profile` alanını tek başına değiştirmek diğer
ayarları üretim değerlerine dönüştürmez.

Yeni kurulumu `./dojo init` ile hazırladıktan sonra, servis başlatmadan önce üretim
örneğini `.runtime/config.json` içine kopyalayın ve `chmod 600` uygulayın.
DNS/HTTPS, yollar, mount'lar ve zamanları kendi ortamınıza uyarlayın.
`secrets.json` dosyasını koruyun. Mevcut kurulumda bütün config'i kopyalamayın:
özellikle `project` ve `version` değişmeden yalnızca yeni ayarları birleştirin.
`./dojo doctor`, ardından planlı pencerede `./dojo start` ve tekrar `doctor` kullanın.

Eski config'lerde yalnızca yeni alanlar pilot varsayılanlarıyla bellekte tamamlanır;
bilinmeyen veya eksik eski zorunlu alanlar reddedilir. Upgrade ayarları korur.
Backup tüm ayarları taşır; restore uygulama sürümünü/secret'ları yedekten alır,
hedef VM'nin kaynak, disk, upload ve timeout ayarlarını korur. Yeni VM'de restore
öncesinde istediğiniz üretim ayarlarını ayrıca uygulayın; eski yedek bunları dayatmaz.

## Aynı RAM'in ortak bütçesi

Compose `mem_limit` değerleri bütün container'a uygulanır; worker limiti her
prefork çocuğu için ayrı değildir. Üretim örneği:

| Hizmet | Üst limit (GiB) | Başlangıç davranışı |
|---|---:|---|
| PostgreSQL | 20 | shared_buffers 8GB; work_mem 8MB; 80 bağlantı |
| Celery worker | 12 | 2 prefork; autoscale min=max=2; prefetch=1 |
| uWSGI web | 8 | 2 süreç × 2 thread; master/stats |
| Valkey | 2 | Kuyruk boşaltması korunur; eviction ile iş atılmaz |
| Celery beat | 1 | Tek scheduler |
| Container nginx | 1 | HTTP erişimi ve upload buffering |
| Initializer | 8 | Migration sırasında; normal işletimde tamamlanmış |

Normal çalışan hizmetlerin limit toplamı 44 GiB; initializer dahil bütün tanımlar
52 GiB. Migration, dump/media ve drain probe'ları farklı aşamalarda çalışır;
`compose run uwsgi` de 8 GiB limitini kullanır. Üretim config doğrulaması bütün
hizmetler için limit ister ve tanım toplamının 52 GiB'yi aşmasını reddeder.
Ek elle açılmış container'lar bu hesaba dahil değildir.

64 GiB olarak sağlanan VM'de normal işletimde kalan yaklaşık 20 GiB'yi OS/daemon
(~4), filesystem cache (~12), bakım ve dalgalanma (~4) için planlayın. Sağlayıcının
“64 GB” birimini `free -h`, `/proc/meminfo` ve `docker info` ile doğrulayın:
64 milyar byte yaklaşık 59,6 GiB'dir; aynı limitlerle normal kalan pay ~15,6 GiB
olur. Gerçek kullanılabilir belleğe göre bütçeyi küçültün. Linux page cache'in
bir kısmı container cgroup'una da yazılır; bu paylar bağımsız RAM havuzları değildir.
Limit rezervasyon değildir; sınıra gelmek OOM ve iş hatası doğurabilir.

PostgreSQL'in `effective_cache_size=24GB` değeri planlayıcı tahminidir, ek 24 GB
ayırmaz. `work_mem` bağlantı başına tek sabit tahsis değildir: aynı sorgunun her
sort/hash işlemi, eşzamanlı oturumlar ve paralel query worker'ları çarpar.
Örneğin 80 oturum × 2 sort × 8 MiB ≈ 1,25 GiB; hash varsayılan çarpanı 2 ile
~2,5 GiB olabilir, daha fazla işlem bunu büyütür. Bu örnek üst sınır değildir.
`max_parallel_workers_per_gather=0` başlangıçta query paralelliğini kapatır;
maintenance paralelliği 2, toplam parallel worker 4, worker process 8'dir.
`maintenance_work_mem=512MB`, `autovacuum_work_mem=256MB`, 3 autovacuum worker
ayrı tüketim yaratır. `max_wal_size=8GB` disk için yumuşak checkpoint hedefidir,
sert WAL kotası değildir. Fsync, WAL güvenliği ve autovacuum kapatılmaz.

Worker ve web sayıları doğrulanan ayrı alanlardır; serbest komut/parametre alınmaz.
PostgreSQL ayarları izinli `postgres_settings` anahtarlarından `postgres -c` ile
uygulanır, restart gerektirenler kontrollü `start` ile etkinleşir. `shm_size=1g`
parallel maintenance için /dev/shm tavanıdır, ek 1 GiB RAM rezervasyonu değildir.

## Veri diskini gerçekten kullanma

Bu repo disk biçimlendirmez, mount oluşturmaz, Docker verisi taşımaz ve host
konfigürasyonu kurmaz. Aşağıdakileri operatör planlı olarak yapar:

1. Disk kimliğini `lsblk -f`, `findmnt` ve depolama envanteriyle doğrulayın.
   Hazır veri filesystem'ini UUID üzerinden `/srv/defectdojo` gibi kalıcı bir mount'a
   bağlayın. Burada biçimlendirme komutu verilmez; veri bulunan disk silinmez.
2. **Yeni ve boş Docker hostunda** mevcut `/etc/docker/daemon.json` ayarlarını
   koruyarak `data-root` değerini `/srv/defectdojo/docker` yapın. Docker'ın mount'tan
   sonra başlamasını systemd `RequiresMountsFor=/srv/defectdojo` ile sağlayın;
   mount yokken OS diskine boş dizin açılmasına izin vermeyin. Reboot sonrası da
   doğrulayın. Mevcut Docker verisi varsa önce envanter/yedek ve ayrı taşıma planı
   hazırlayın; `data-root` değiştirmek eski volume'leri otomatik taşımaz.
3. `docker info --format '{{.DockerRootDir}}'` ve
   `docker info --format '{{json .DriverStatus}}'` okuyun. Containerd image store
   (`io.containerd.snapshotter.v1`) kullanıldığında Docker `data-root` bütün imaj
   katmanlarını taşımaz. `systemctl cat docker containerd`, containerd'nin gerçek
   config/start argümanları ve `containerd config dump` ile **kullanılan** image-store
   `root` dizinini saptayın. Yeni hostta bu root'u da veri diskine planlayın;
   çalışan hostta habersiz taşımayın. `image_storage_dir` alanına doğrulanmış
   gerçek yolu yazın. CLI bunu daemon config'inden keşfetmez; bildirilen dizinin
   filesystem'ini kontrol eder, snapshotter varken boş bırakılmasını reddeder.
4. `findmnt -T /srv/defectdojo/docker`, gerekirse image-store yolu için `findmnt -T`,
   `df -hT` ve `df -i` ile disk/boş alan/inode durumunu doğrulayın. Symlink yerine
   kanonik mount yolları kullanın. State ve yerel backup staging'i de veri diskinde
   tutmak için `--state /srv/defectdojo/state` ve örnekteki `backup_dir` seçilebilir.
   Aynı state yolunu bütün komutlarda ve systemd `ExecStart`'ta kullanın.
5. Kurulumdan sonra her volume'ün `docker volume inspect VOLUME` içindeki
   `Mountpoint` alanını, çalışan container'ın `docker inspect CONTAINER` içindeki
   `Mounts` alanını ve `findmnt -T MOUNTPOINT` sonucunu karşılaştırın.
   `doctor` da beklenen named volume adını/target'ını ve gerçek filesystem'i denetler.
   PostgreSQL 18 mount'u **/var/lib/postgresql**, PGDATA
   **/var/lib/postgresql/18/docker** kalır. Eski subdirectory/raw datadir'i taşımak
   yerine desteklenen logical backup/restore kullanılır.

`doctor/preflight` state, DockerRootDir, bildirilen image store, PostgreSQL/media/
Valkey volume'leri, yerel staging ve ayrı yedek hedefindeki gerçek boş alanı
loglar. Root-owned Docker volume boyutları sabit PostgreSQL imajında geçici,
ağsız, salt okunur `du` ile ölçülür; mevcut volume oluşturulmaz/değiştirilmez.
Komutlar yerel Unix-socket daemon ister; başka VM'deki Docker context desteklenmez.
İlk kuruluma ait volume'ler henüz yoksa DockerRootDir filesystem'i denetlenir.

Üretim örneğinde her filesystem için `disk_reserve_gib=50`; backup yeni kopya için
`max(ölçülen DB+media, backup_estimate_gib=50)` GiB ister. Aynı diskteki staging ve
ayrı kopya talepleri toplanır; retention'dan gelecek boş alan önceden harcanmaz.
Restore için eski nesil **zaten boş alandan düşülmüştür**; üstüne yeni nesil için
`1,5 × max(mevcut ham veri, yedekteki ham kaynak ölçüsü, restore_estimate_gib,
arşiv byte toplamı × 4)` ve rezerv istenir. Yeni nesil filesystem'leri ayrı
kontrol edilir. Günlük backup drain bittikten sonra veri boyutunu ve alanı tekrar
ölçer; uzun import büyümesi ilk ölçümle sınırlı kalmaz. 200 GiB restore tahmini örnektir: sıkıştırma oranı, yeni indeksler,
WAL, geçici dosyalar veya farklı veri yerleşimi bunu aşabilir. Eski yedeklerde ham
ölçü yoktur; özellikle boş VM'ye restore öncesi `restore_estimate_gib` değerini
bilinen açılmış DB+media ölçüsüne göre yükseltin. Kontrol alan rezervasyonu veya
sürekli izleme değildir; başka işler diski doldurabilir. 750 GB tek başına iki
nesil + staging + WAL + imaj + büyüme için garanti değildir; gerekirse önce genişletin.

## VM dışı backup mount'u

Üretim örneği NFS/CIFS/SSHFS benzeri bir uzak filesystem'in
`/mnt/dojo-backup` üzerinde önceden bağlı olmasını ister. Mount kökü
`backup_copy_mount`, içindeki 0700 hedef dizin `backup_copy_dir` olur.
NFS kimlik eşlemesi/izinleri operatör hesabıyla sınanmalıdır. `mountpoint`,
`findmnt -T /mnt/dojo-backup/defectdojo`, `df -hT` ile kaynak sunucuyu doğrulayın;
VM'nin kendi diskine açılmış normal dizin bu koşulu sağlamaz.

CLI beklenen mount yoksa backup/upgrade bakımından **önce** reddeder. Kopyalamadan
önce ve sonra da mount'u doğrular; yerel fallback'i başarılı uzak yedek saymaz.
Pilot config'de `backup_copy_dir` kullanılıyorsa artık `backup_copy_mount` da gerekir.
`doctor` yedek hedefini de denetler; `start --resume` ve restore kullanılmayan uzak
yedek hedefine bağımlı değildir. Kopya hatasında yerel uygulamayı mount dönmeden
yeniden açabilirsiniz; yeni backup/upgrade mount doğrulanana kadar reddedilir.
Yedekler şifrelenmez; güvenli mount, disk şifrelemesi, VM dışı retention ve erişim
politikası operatöre aittir. Systemd backup unit'ine kendi veri/yedek mount'larınız
için `RequiresMountsFor=` ekleyin; CLI kontrolü ayrıca korunur.

## Büyük raporların üç katmanı

| Alan | Pilot | Üretim örneği | Etkisi |
|---|---:|---:|---|
| `upload_max_mb` | 800 | 800 | Container nginx request body, multipart payı dahil |
| `upload_timeout` | 60 sn | 300 sn | nginx client_body_timeout; I/O beklemesi |
| `request_timeout` | 1800 sn | 3600 sn | nginx uwsgi read/send; uWSGI socket-timeout |
| `data_upload_max_memory_mb` | 8 | 8 | Django non-file request body limiti |

Sabit sürümler `TemporaryFileUploadHandler` kullanır; Django memory limiti dosya
upload kotası değildir ve büyük XML için 800 MB RAM'e yükseltilmemelidir.
Upload'ın disk geçici kopyaları, host proxy buffering'i ve parse sırasında açılan
XML belleği ayrıca ölçülür. Container writable layer/tmp Docker veri diskindedir;
host proxy temp dizinini de `nginx -T` ile bulun ve diskini denetleyin.

[Host nginx örneğini](nginx-https.conf.example) config değerleriyle birlikte
uyarlayın; `client_max_body_size`, `client_body_timeout`, `proxy_read_timeout`,
`proxy_send_timeout` eşleşmelidir. Önce hostta `nginx -t`, sonra operatörce reload;
container ayarlarını `./dojo start` uygular. `./dojo doctor` container `nginx -T`,
Django gerçek ayarı, uWSGI stats süreç/thread sayısı ve PostgreSQL gerçek ayarlarını
karşılaştırır; beklenen host değerlerini loglar. Host nginx dosyalarını CLI okumaz
ve host proxy'yi otomatik doğruladığını iddia etmez. Host `nginx -T` + gerçek rapor
uçtan uca doğrulaması zorunludur. Geçersiz/uygulanmamış ayarda doctor hata verir.

Bu süreler I/O bekleme süreleridir; toplam import süresinin üst sınırı değildir.
Harakiri, task time limit veya SIGKILL eklenmez. Client/proxy timeout sonrası import
sunucuda sürebilir; Test/task sonucunu kontrol etmeden yeniden göndermeyin.
İlk pilotta bütün kapsamları **sırayla** import edin, Info bulgularını dahil edin.
Sabit kapsam/parser aynı Test'e reimport edilir. Başarısız, eksik veya kapsamı
değişmiş taramalar eski bulguları kapatamaz: `close_old_findings=false` korunur.

## Bakım zamanları ve kesintinin ölçülmesi

| Ayar | Pilot | Üretim örneği |
|---|---:|---:|
| `command_timeout` | 300 sn | 300 sn |
| `backup_timeout` | 7200 sn | 21600 sn (6 saat) |
| `restore_timeout` | 14400 sn | 43200 sn (12 saat) |
| `drain_timeout` | 900 sn | 14400 sn (4 saat) |
| `migration_timeout` | 1800 sn | 21600 sn (6 saat) |
| `health_timeout` | 180 sn | 600 sn |

Backup/restore timeout'ları dump, media aktarımı ve dump-list doğrulamasının her
Docker komutuna ayrı uygulanır; bütün operasyonun toplam deadline'ı değildir.
Checksum/media kontrolü ve filesystem kopyası bu komut deadline'ına tabi değildir.
Drain timeout her servis bekleyişi ve kuyruk boşalma aşamasında ayrı kullanılabilir;
toplam bakım süresi tablodaki tek bir sayıyla sınırlanmaz. Initializer timeout'unda
migration container'ı öldürülmez; aynı initializer `start --resume` ile beklenir.
Drain işler bitmeden writer'ları zorla öldürmez. Timeout sonrası kalan tek seferlik
Docker komut/container'larını `status`/özel loglarla inceleyin; körlemesine retry
veya doğrudan Compose müdahalesi yapmayın.

Backup erişimi kapatır. Ölçüm ingress kapatılmaya başlanmadan hemen önce başlar;
drain + dump + media + doğrulama + VM dışı kopya + yeniden aktivasyon/migration +
sağlık kontrolleri bitene kadar sürer. Upgrade'de aynı ölçüm yükseltmeyi de kapsar.
UTC başlangıç/bitiş ve toplam saniye `.runtime/maintenance.json` (özel state
kullanılıyorsa onun altında) ve logda saklanır. Hata/retry operatör bekleme süresi
dahil aynı açık pencereyi sürdürür; başarısız işlem tamamlanmış gibi raporlanmaz.
`status` açık pencerenin o anki süresini de raporlar.
Bu, ağdan ölçülen kesin erişilebilirlik değil muhafazakâr bakım penceresidir.

Backup systemd örneğinde `TimeoutStartSec=infinity`, `TimeoutStopSec=infinity`:
CLI aşama sürelerini yönetir; systemd uzun işi 4 saatte zorla kesmez. Manuel
`systemctl stop/restart` veya host reboot ile işi kesmeyin; journal ve bakım durumunu
izleyin. Alarm/izleme ile aşırı süreyi operatöre bildirin. Hız kazanmak için işi
öldürmek yerine pencereyi ve kaynakları ölçümle planlayın.

## Uygulanabilir pilot kabul planı

1. İzole prova VM'sinde aynı disk/mount/limitlerle `doctor` çalıştırın. İlk ve
   reboot sonrası DockerRootDir, image store, named/live mount, RAM/boş alan/inode
   kanıtlarını kaydedin. Backup mount'unu **yalnız prova ortamında** ayırıp backup'ın
   erişimi kapatmadan hata verdiğini sınayın; mount'u geri getirip tekrar doğrulayın.
2. Kurumdan tam, başarılı bir OpenVAS v2 XML/CSV raporuyla küçük sabit kapsamı import
   edin, sonra kapsamları sırayla büyütün. Her raporda boyut, süre, Test ID,
   Info dahil bulgu/endpoint sayıları, HTTP ve Celery sonucunu kaydedin. Aynı raporu
   aynı Test'e tekrar vererek çoğalma/dedup sonucunu ölçün. Sonraki tam raporda
   yeni/aynı/giderilmiş bulguları karşılaştırın; kapanışı ancak kanıtla açın.
   Eksik/başarısız örnekte `close_old_findings=false` ile eski bulguların açık
   kaldığını uygulamada doğrulayın. Başlangıçta tüm importlar sırayla olsun.
3. Her adımda `docker stats --no-stream`, `free -h`, `vmstat 5`, varsa `iostat -xz 5`,
   disk/WAL/temp büyümesi ve Celery kuyruk/aktif işlerini kaydedin. PostgreSQL
   `pg_stat_database` içinden temp_bytes/deadlocks, `pg_stat_activity` içinden
   bağlantı/beklemeleri, container OOM/restart olaylarını inceleyin. Boşta, import,
   reimport ve bakım tepe değerlerini ayrı tutun; cache'in RAM kullanımını da sayın.
4. Yaklaşık 500.000 **birikmiş** bulguya kademeli ulaşıp UI/API kullanırken aylık
   reimport provası yapın; hedef RTO/RPO ve bakım penceresini kurum olarak belirleyin.
   Gerekiyorsa önce worker 2→3 veya web süreç 2→3 değiştirin; her seferinde tek
   değişkenle aynı raporu tekrar ölçün. DB lock, RAM, I/O veya kuyruk kötüleşirse
   geri alın. 16 vCPU olması worker'ı 16 yapma gerekçesi değildir.
5. Gerçek boyutta backup alın. Açık bir import sürerken drain'in onu beklediğini
   ve raporlanan **toplam** kesintiyi kaydedin. Dump/media/VM dışı kopya boyutlarını,
   mevcut ham DB+media, kalan alanı ve her aşamanın journal zamanlarını tutun.
   Kabul edilen pencereyi aşarsa işlemi öldürmeyin; sonraki pencereyi büyütün.
6. VM dışı kopyayı ayrı prova state/projesine açık onayla restore edin. Eski/yeni
   volume nesilleri birlikte dururken gerçek tepe diski, restore/migration süresi,
   DB satırları, media hash'leri, secret ile açılan credential'lar, admin/HTTP ve
   gerçek import/reimport sonucunu doğrulayın. `doctor` geçmeden eski nesilleri
   temizlemeyin. Eski config içeren yedeği de prova edin; boyut tahminini uyarlayın.
7. Aynı veri kopyasıyla izinli upgrade yolunu prova edin; bütün yeni ayarların
   kaldığını `doctor` ile doğrulayın. Ölçüm, log, rapor kimliği ve kabul kararını
   tarihle kaydedin. Başarılı küçük örnek, 500.000 bulguluk kapasite kanıtı değildir.

## Doğrulanan upstream seçenekleri

3.4.201 (`1b10e779eeeb624d75eb328f58fa8424ec09d000`) ve 3.4.202
(`8cbbe0d8c82d007662d270ccd7ab3365e2c84a83`) kaynakları kontrol edildi.
[uWSGI entrypoint](https://github.com/DefectDojo/django-DefectDojo/blob/8cbbe0d8c82d007662d270ccd7ab3365e2c84a83/docker/entrypoint-uwsgi.sh)
`DD_UWSGI_NUM_OF_PROCESSES`, `DD_UWSGI_NUM_OF_THREADS`, `DD_UWSGI_EXTRA_ARGS` kullanır.
[nginx şablonu](https://github.com/DefectDojo/django-DefectDojo/blob/8cbbe0d8c82d007662d270ccd7ab3365e2c84a83/nginx/nginx.conf)
body limitini 800m, read timeout'u 1800 sabitler; entrypoint bunlar için env okumaz.
Manager yalnız container içindeki şablonu kontrollü ve tekrarlanabilir değiştirir;
release paketlerinin checksum'ları değişmez.
[Django ayarları](https://github.com/DefectDojo/django-DefectDojo/blob/8cbbe0d8c82d007662d270ccd7ab3365e2c84a83/dojo/settings/settings.dist.py)
upload handler ve `DD_DATA_UPLOAD_MAX_MEMORY_SIZE` davranışını tanımlar.
[PostgreSQL 18 kaynak ayarları](https://www.postgresql.org/docs/18/runtime-config-resource.html)
work_mem çarpanlarını ve bakım belleğini açıklar.
[Docker image-store yerleşimi](https://docs.docker.com/engine/storage/containerd/)
ve [daemon data-root](https://docs.docker.com/engine/daemon/)
containerd store ile volume verisinin ayrı konumlanabileceğini açıklar.
