"""Decodage NetFlow v5 / v9 et IPFIX.

POURQUOI DU FLUX EXPORTE, ET PAS UN MIROIR DE PORT
--------------------------------------------------
Le controleur ne doit jamais etre sur le chemin des paquets. Pour savoir de quoi
est fait le trafic, il reste deux voies : dupliquer le trafic vers une sonde
(port mirroring), ou demander aux routeurs de RESUMER ce qu'ils ont vu.

Le miroir est exclu. Il recopie chaque octet sur un lien de collecte : sur une
sortie internet a 10 Gbit/s, c'est 10 Gbit/s de plus a transporter, dans les
deux sens, a travers le coeur qu'on cherchait justement a ne pas charger.

Le flux exporte tient dans quelques dizaines de kbit/s : un datagramme UDP
resume des milliers de conversations. C'est la seule mesure de volume qui passe
a l'echelle d'un operateur sans lui couter un lien.

OU ON ECOUTE. En amont du coeur (la sortie internet) et au PoP -- aux deux
extremites, jamais au milieu. Cf. NETFLOW_ACCOUNTING_VANTAGE : le meme octet
etant vu aux deux endroits, la consommation se lit depuis un seul.

CE MODULE NE FAIT QUE DECODER. Pas de socket, pas de base, pas d'horloge : des
octets entrent, des flux sortent. C'est ce qui le rend testable avec des
datagrammes reels captures sur le terrain.
"""

from __future__ import annotations

import ipaddress
import logging
import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

V5_HEADER = struct.Struct("!HHIIIIBBH")
V5_RECORD = struct.Struct("!4s4s4sHHIIIIHHBBBBHHBBH")
V9_HEADER = struct.Struct("!HHIIII")
IPFIX_HEADER = struct.Struct("!HHIII")

#: Champs IPFIX / NetFlow v9 que l'on sait lire. Tout le reste est saute a la
#: bonne longueur : un exporteur qui envoie des champs constructeur ne doit pas
#: faire echouer le decodage du datagramme entier.
FIELDS: dict[int, str] = {
    1: "bytes",  # octetDeltaCount
    2: "packets",  # packetDeltaCount
    4: "protocol",  # protocolIdentifier
    5: "tos",  # ipClassOfService
    7: "src_port",  # sourceTransportPort
    8: "src",  # sourceIPv4Address
    10: "input_snmp",  # ingressInterface
    11: "dst_port",  # destinationTransportPort
    12: "dst",  # destinationIPv4Address
    14: "output_snmp",  # egressInterface
    23: "bytes",  # postOctetDeltaCount (meme flux, apres le point d'observation)
    24: "packets",  # postPacketDeltaCount
    27: "src",  # sourceIPv6Address
    28: "dst",  # destinationIPv6Address
    58: "vlan",  # vlanId
    59: "post_vlan",  # postVlanId
    85: "bytes",  # octetTotalCount
    86: "packets",  # packetTotalCount
}

#: Champs dont la valeur est une adresse et non un entier.
ADDRESS_FIELDS = frozenset({"src", "dst"})

VARIABLE_LENGTH = 0xFFFF


class NetflowParseError(ValueError):
    """Le datagramme n'est pas un export NetFlow exploitable."""


@dataclass(frozen=True)
class Flow:
    """Une conversation resumee par l'exporteur.

    ``vlan`` n'est presque jamais renseigne par un routeur purement L3 : il ne
    l'est que lorsque l'exporteur voit l'etiquette (pont, sous-interface). On
    ne l'invente pas quand il manque -- une VLAN devinee serait pire que pas de
    VLAN du tout.
    """

    src: str
    dst: str
    src_port: int = 0
    dst_port: int = 0
    protocol: int = 0
    octets: int = 0
    packets: int = 0
    vlan: int | None = None
    tos: int | None = None
    input_snmp: int | None = None
    output_snmp: int | None = None


@dataclass(frozen=True)
class DecodedPacket:
    version: int
    flows: tuple[Flow, ...]
    sequence: int = 0
    domain: int = 0
    export_time: datetime | None = None
    templates_learned: int = 0
    #: Echantillonnage annonce dans l'en-tete v5. 0 = non renseigne.
    sampling_interval: int = 0


@dataclass
class _Template:
    fields: tuple[tuple[int, int], ...]
    length: int
    is_option: bool = False
    #: Un modele a longueur variable ne peut pas etre saute a l'aveugle ; on le
    #: signale pour lire ses enregistrements un par un.
    variable: bool = False


@dataclass
class NetflowDecoder:
    """Decode les datagrammes et MEMORISE les modeles v9 / IPFIX.

    LE POINT DELICAT DE NETFLOW V9 ET D'IPFIX : les donnees sont illisibles sans
    le modele qui les decrit, et ce modele arrive dans un datagramme SEPARE,
    reemis toutes les quelques minutes. Un collecteur qui redemarre jette donc
    des flux pendant ce delai -- c'est normal, et c'est pour cela qu'on compte
    les enregistrements ignores plutot que de les taire : "je ne recois rien" et
    "je recois sans savoir lire" appellent deux gestes tres differents.

    Le cache est indexe par (exporteur, domaine, modele) : deux routeurs peuvent
    tres bien utiliser le meme numero de modele pour des champs differents.
    """

    templates: dict[tuple[str, int, int], _Template] = field(default_factory=dict)
    #: Enregistrements recus sans modele connu, depuis le demarrage.
    orphan_records: int = 0

    def decode(self, data: bytes, exporter: str) -> DecodedPacket:
        if len(data) < 4:
            raise NetflowParseError(f"datagramme trop court ({len(data)} octets)")
        version = struct.unpack_from("!H", data, 0)[0]
        if version == 5:
            return self._decode_v5(data)
        if version == 9:
            return self._decode_v9(data, exporter)
        if version == 10:
            return self._decode_ipfix(data, exporter)
        raise NetflowParseError(f"version NetFlow non supportee : {version}")

    # ------------------------------------------------------------------- v5
    def _decode_v5(self, data: bytes) -> DecodedPacket:
        if len(data) < V5_HEADER.size:
            raise NetflowParseError("en-tete v5 tronque")
        (
            _version,
            count,
            _uptime,
            unix_secs,
            _unix_nsecs,
            sequence,
            _engine_type,
            _engine_id,
            sampling,
        ) = V5_HEADER.unpack_from(data, 0)
        if count > 30:
            # Un datagramme v5 porte au plus 30 enregistrements. Au-dela, le
            # compteur ment (datagramme tronque ou corrompu) : on prefere lire
            # ce que la taille reelle autorise plutot que sortir du tampon.
            count = 30
        flux: list[Flow] = []
        offset = V5_HEADER.size
        for _ in range(count):
            if offset + V5_RECORD.size > len(data):
                break
            champs = V5_RECORD.unpack_from(data, offset)
            offset += V5_RECORD.size
            flux.append(
                Flow(
                    src=str(ipaddress.IPv4Address(champs[0])),
                    dst=str(ipaddress.IPv4Address(champs[1])),
                    src_port=champs[9],
                    dst_port=champs[10],
                    protocol=champs[13],
                    octets=champs[6],
                    packets=champs[5],
                    tos=champs[14],
                    input_snmp=champs[3],
                    output_snmp=champs[4],
                )
            )
        return DecodedPacket(
            version=5,
            flows=tuple(flux),
            sequence=sequence,
            export_time=_epoch(unix_secs),
            # Les 2 octets portent un mode sur 2 bits et l'intervalle sur 14.
            sampling_interval=sampling & 0x3FFF,
        )

    # ------------------------------------------------------------------- v9
    def _decode_v9(self, data: bytes, exporter: str) -> DecodedPacket:
        if len(data) < V9_HEADER.size:
            raise NetflowParseError("en-tete v9 tronque")
        _version, _count, _uptime, unix_secs, sequence, source_id = V9_HEADER.unpack_from(data, 0)
        flux, appris = self._walk_sets(
            data, V9_HEADER.size, exporter, source_id, template_set=0, option_set=1
        )
        return DecodedPacket(
            version=9,
            flows=tuple(flux),
            sequence=sequence,
            domain=source_id,
            export_time=_epoch(unix_secs),
            templates_learned=appris,
        )

    # ---------------------------------------------------------------- ipfix
    def _decode_ipfix(self, data: bytes, exporter: str) -> DecodedPacket:
        if len(data) < IPFIX_HEADER.size:
            raise NetflowParseError("en-tete IPFIX tronque")
        _version, length, export_time, sequence, domain = IPFIX_HEADER.unpack_from(data, 0)
        # La longueur annoncee fait foi quand elle tient dans le tampon : un
        # datagramme peut etre suivi de bourrage.
        utile = data[:length] if IPFIX_HEADER.size <= length <= len(data) else data
        flux, appris = self._walk_sets(
            utile, IPFIX_HEADER.size, exporter, domain, template_set=2, option_set=3
        )
        return DecodedPacket(
            version=10,
            flows=tuple(flux),
            sequence=sequence,
            domain=domain,
            export_time=_epoch(export_time),
            templates_learned=appris,
        )

    # --------------------------------------------------------------- commun
    def _walk_sets(
        self,
        data: bytes,
        offset: int,
        exporter: str,
        domain: int,
        *,
        template_set: int,
        option_set: int,
    ) -> tuple[list[Flow], int]:
        ipfix = template_set == 2
        flux: list[Flow] = []
        appris = 0
        while offset + 4 <= len(data):
            set_id, set_length = struct.unpack_from("!HH", data, offset)
            if set_length < 4 or offset + set_length > len(data):
                # Longueur incoherente : continuer reviendrait a lire du bruit
                # comme si c'etaient des octets factures.
                break
            corps = data[offset + 4 : offset + set_length]
            offset += set_length
            if set_id == template_set:
                appris += self._read_templates(corps, exporter, domain, ipfix=ipfix)
            elif set_id == option_set:
                appris += self._read_option_templates(corps, exporter, domain, ipfix=ipfix)
            elif set_id >= 256:
                flux.extend(self._read_data(corps, exporter, domain, set_id, ipfix=ipfix))
            # 1 <= set_id < 256 en IPFIX : reserve, rien a lire.
        return flux, appris

    def _read_templates(self, corps: bytes, exporter: str, domain: int, *, ipfix: bool) -> int:
        offset = 0
        appris = 0
        while offset + 4 <= len(corps):
            template_id, count = struct.unpack_from("!HH", corps, offset)
            offset += 4
            champs, offset = self._read_fields(corps, offset, count, ipfix=ipfix)
            if champs is None:
                break
            self._store(exporter, domain, template_id, champs, is_option=False)
            appris += 1
        return appris

    def _read_option_templates(
        self, corps: bytes, exporter: str, domain: int, *, ipfix: bool
    ) -> int:
        """Lit un modele d'OPTIONS, uniquement pour savoir sauter ses donnees.

        On n'exploite pas leur contenu (compteurs d'exporteur, table
        d'interfaces). Mais sans leur longueur, leurs enregistrements seraient
        lus comme des flux : des octets inventes, attribues a de vraies fiches.
        """
        offset = 0
        appris = 0
        while offset + 4 <= len(corps):
            if ipfix:
                if offset + 6 > len(corps):
                    break
                template_id, count, _scope = struct.unpack_from("!HHH", corps, offset)
                offset += 6
                champs, offset = self._read_fields(corps, offset, count, ipfix=True)
                if champs is None:
                    break
            else:
                if offset + 6 > len(corps):
                    break
                template_id, scope_len, option_len = struct.unpack_from("!HHH", corps, offset)
                offset += 6
                total = scope_len + option_len
                if total % 4 or offset + total > len(corps):
                    break
                champs, offset = self._read_fields(corps, offset, total // 4, ipfix=False)
                if champs is None:
                    break
            self._store(exporter, domain, template_id, champs, is_option=True)
            appris += 1
        return appris

    def _read_fields(
        self, corps: bytes, offset: int, count: int, *, ipfix: bool
    ) -> tuple[tuple[tuple[int, int], ...] | None, int]:
        champs: list[tuple[int, int]] = []
        for _ in range(count):
            if offset + 4 > len(corps):
                return None, offset
            type_id, longueur = struct.unpack_from("!HH", corps, offset)
            offset += 4
            if ipfix and type_id & 0x8000:
                # Champ constructeur : 4 octets de numero d'entreprise suivent.
                if offset + 4 > len(corps):
                    return None, offset
                offset += 4
                type_id = 0  # inconnu, donc saute a la bonne longueur
            champs.append((type_id, longueur))
        return tuple(champs), offset

    def _store(
        self,
        exporter: str,
        domain: int,
        template_id: int,
        champs: tuple[tuple[int, int], ...],
        *,
        is_option: bool,
    ) -> None:
        variable = any(longueur == VARIABLE_LENGTH for _type, longueur in champs)
        longueur = sum(taille for _type, taille in champs if taille != VARIABLE_LENGTH)
        self.templates[(exporter, domain, template_id)] = _Template(
            fields=champs, length=longueur, is_option=is_option, variable=variable
        )

    def _read_data(
        self, corps: bytes, exporter: str, domain: int, template_id: int, *, ipfix: bool
    ) -> list[Flow]:
        modele = self.templates.get((exporter, domain, template_id))
        if modele is None:
            self.orphan_records += 1
            return []
        if modele.is_option or modele.length <= 0:
            return []
        flux: list[Flow] = []
        offset = 0
        # Le bourrage de fin (jusqu'a 3 octets) ne fait jamais un enregistrement.
        while offset + modele.length <= len(corps):
            valeurs, offset = self._read_record(corps, offset, modele, ipfix=ipfix)
            if valeurs is None:
                break
            flow = _to_flow(valeurs)
            if flow is not None:
                flux.append(flow)
        return flux

    def _read_record(
        self, corps: bytes, offset: int, modele: _Template, *, ipfix: bool
    ) -> tuple[dict[str, object] | None, int]:
        valeurs: dict[str, object] = {}
        for type_id, longueur in modele.fields:
            if ipfix and longueur == VARIABLE_LENGTH:
                if offset >= len(corps):
                    return None, offset
                longueur = corps[offset]
                offset += 1
                if longueur == 255:
                    if offset + 2 > len(corps):
                        return None, offset
                    longueur = struct.unpack_from("!H", corps, offset)[0]
                    offset += 2
            if offset + longueur > len(corps):
                return None, offset
            brut = corps[offset : offset + longueur]
            offset += longueur
            nom = FIELDS.get(type_id)
            if nom is None:
                continue
            if nom in ADDRESS_FIELDS:
                if longueur in (4, 16):
                    valeurs[nom] = str(ipaddress.ip_address(brut))
                continue
            valeurs[nom] = int.from_bytes(brut, "big")
        return valeurs, offset


def _to_flow(valeurs: dict[str, object]) -> Flow | None:
    src = valeurs.get("src")
    dst = valeurs.get("dst")
    if not isinstance(src, str) or not isinstance(dst, str):
        # Un enregistrement sans adresses ne peut etre rattache a personne.
        return None
    return Flow(
        src=src,
        dst=dst,
        src_port=_int(valeurs.get("src_port")),
        dst_port=_int(valeurs.get("dst_port")),
        protocol=_int(valeurs.get("protocol")),
        octets=_int(valeurs.get("bytes")),
        packets=_int(valeurs.get("packets")),
        vlan=_opt_int(valeurs.get("vlan")) or _opt_int(valeurs.get("post_vlan")),
        tos=_opt_int(valeurs.get("tos")),
        input_snmp=_opt_int(valeurs.get("input_snmp")),
        output_snmp=_opt_int(valeurs.get("output_snmp")),
    )


def _int(value: object) -> int:
    return int(value) if isinstance(value, int) else 0


def _opt_int(value: object) -> int | None:
    return int(value) if isinstance(value, int) and value else None


def _epoch(seconds: int) -> datetime | None:
    if not seconds:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
