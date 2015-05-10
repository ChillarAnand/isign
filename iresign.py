import construct
import distutils
import hashlib
import subprocess
import os
import OpenSSL
from optparse import OptionParser
from hexdump import hexdump

import macho
import macho_cs


OPENSSL = os.getenv('OPENSSL', distutils.spawn.find_executable('openssl'))


def sign(data, signer_cert_file, signer_key_file, cert_file):
    proc = subprocess.Popen("%s cms"
                            " -sign -binary -nosmimecap"
                            " -certfile %s"
                            " -signer %s"
                            " -inkey %s"
                            " -keyform pkcs12 "
                            " -outform DER" %
                            (OPENSSL,
                             cert_file,
                             signer_cert_file,
                             signer_key_file),
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            shell=True)
    proc.stdin.write(data)
    out, err = proc.communicate()
    print err
    return out


def print_parsed_asn1(data):
    proc = subprocess.Popen('openssl asn1parse -inform DER -i',
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            shell=True)
    proc.stdin.write(data)
    out, err = proc.communicate()
    print out


def get_codesig_blob(codesig_cons, magic):
    for index in codesig_cons.data.BlobIndex:
        if index.blob.magic == magic:
            return index.blob
    raise KeyError(magic)

def make_arg(data_type, arg):
    if data_type.name == 'Data':
        return construct.Container(data=arg,
                                   length=len(arg))
    elif data_type.name.lower() == 'expr':
        return make_expr(*arg)
    elif data_type.name == 'slot':
        if arg == 'leafCert':
            return 0
        return arg
    elif data_type.name == 'Match':
        matchOp = arg[0]
        data = None
        if len(arg) > 1:
            data = construct.Container(data=arg[1],
                                       length=len(arg[1]))
        return construct.Container(matchOp=matchOp, Data=data)
    print data_type
    print data_type.name
    print arg
    assert 0

def make_expr(op, *args):
    op = "op" + op
    data = None
    data_type = macho_cs.expr_args.get(op)
    if isinstance(data_type, macho_cs.Sequence):
        data = [make_arg(dt, arg) for dt, arg in zip(data_type.subcons, args)]
    elif data_type:
        data = make_arg(data_type, args[0])
    return construct.Container(op=op,
                               data=data)

def make_designated_requirement():
    req = construct.Container(kind=1,
                              expr=make_expr(
        'And',
        ('Ident', 'ca.michaelhan.NativeIOSTestApp'),
        ('And',
         ('AppleGenericAnchor',),
         ('And',
          ('CertField', 'leafCert', 'subject.CN', ['matchEqual', 'iPhone Developer: Steven Hazel (DU2T223MY8)']),
          ('CertGeneric', 1, '*\x86H\x86\xf7cd\x06\x02\x01', ['matchExists'])))))
    print req
    req_data = macho_cs.Requirement.build(req)
    return construct.Container(
        sb_start=0,
        count=1,
        BlobIndex=[construct.Container(type='kSecDesignatedRequirementType',
                                       offset=20,
                                       blob=construct.Container(magic='CSMAGIC_REQUIREMENT',
                                                                length=len(req_data) + 8,
                                                                data="",
                                                                bytes=req_data))
                   ]
        )

def make_basic_codesig(entitlements_file):
    ident = 'ca.michaelhan.NativeIOSTestApp' + '\x00'
    teamID = 'fake' + '\x00'
    empty_hash = "\x00" * 20
    cd = construct.Container(cd_start=None,
                             version=0x20200,
                             flags=0,
                             identOffset=52,
                             nSpecialSlots=5,
                             nCodeSlots=0,
                             codeLimit=54400,
                             hashSize=20,
                             hashType=1,
                             spare1=0,
                             pageSize=12,
                             spare2=0,
                             ident=ident,
                             scatterOffset=0,
                             teamIDOffset=52 + len(ident),
                             teamID=teamID,
                             hashOffset=52 + (20 * 5) + len(ident) + len(teamID),
                             hashes=[empty_hash, empty_hash, empty_hash, empty_hash, empty_hash],
                             )
    print cd
    cd_data = macho_cs.CodeDirectory.build(cd)
    print len(cd_data)
    print hexdump(cd_data)

    offset = 44
    cd_index = construct.Container(type=0,
                                   offset=offset,
                                   blob=construct.Container(magic='CSMAGIC_CODEDIRECTORY',
                                                            length=len(cd_data) + 8,
                                                            data=cd,
                                                            bytes=cd_data,
                                                            ))
    offset += cd_index.blob.length
    reqs_sblob = make_designated_requirement()
    print reqs_sblob
    reqs_sblob_data = macho_cs.Entitlements.build(reqs_sblob)
    requirements_index = construct.Container(type=2,
                                             offset=offset,
                                             blob=construct.Container(magic='CSMAGIC_REQUIREMENTS',
                                                                      length=len(reqs_sblob_data) + 8,
                                                                      data="",
                                                                      bytes=reqs_sblob_data,
                                                                      ))
    offset += requirements_index.blob.length + 8

    entitlements_bytes = open(entitlements_file, "rb").read()
    entitlements_index = construct.Container(type=5,
                                            offset=offset,
                                            blob=construct.Container(magic='CSMAGIC_ENTITLEMENT',
                                                                     length=len(entitlements_bytes) + 8,
                                                                     data="",
                                                                     bytes=entitlements_bytes
                                                                     ))
    offset += entitlements_index.blob.length
    sigwrapper_index = construct.Container(type=65536,
                                           offset=offset,
                                           blob=construct.Container(magic='CSMAGIC_BLOBWRAPPER',
                                                                    length=0 + 8,
                                                                    data="",
                                                                    bytes="",
                                                                    ))
    indicies = [cd_index,
                requirements_index,
                entitlements_index,
                sigwrapper_index]

    superblob = construct.Container(
        sb_start=0,
        count=len(indicies),
        BlobIndex=indicies)
    print superblob
    data = macho_cs.SuperBlob.build(superblob)
    print hexdump(data)

    chunk = macho_cs.Blob.build(construct.Container(
        magic="CSMAGIC_EMBEDDED_SIGNATURE",
        length=len(data) + 8,
        data=data,
        bytes=data))
    return macho_cs.Blob.parse(chunk)


def resign_cons(codesig_cons, entitlements_file, signer_cert_file, signer_key_file, cert_file):
    print "entitlements:"
    entitlements = get_codesig_blob(codesig_cons, 'CSMAGIC_ENTITLEMENT')
    entitlements_data = macho_cs.Blob_.build(entitlements)
    print hashlib.sha1(entitlements_data).hexdigest()
    entitlements.bytes = open(entitlements_file, "rb").read()
    entitlements.length = len(entitlements.bytes) + 8
    entitlements_data = macho_cs.Blob_.build(entitlements)
    print hashlib.sha1(entitlements_data).hexdigest()
    print

    print "requirements:"
    requirements = get_codesig_blob(codesig_cons, 'CSMAGIC_REQUIREMENTS')
    #print hexdump(requirements.bytes.value)
    print hashlib.sha1(requirements.bytes.value).hexdigest()
    signer_key_data = open(os.path.expanduser(signer_key_file), "rb").read()
    signer_p12 = OpenSSL.crypto.load_pkcs12(signer_key_data)
    signer_cn = dict(signer_p12.get_certificate().get_subject().get_components())['CN']
    cn = requirements.data.BlobIndex[0].blob.data.expr.data[1].data[1].data[0].data[2].Data
    cn.data = signer_cn
    cn.length = len(cn.data)
    requirements.data.BlobIndex[0].blob.bytes = macho_cs.Requirement.build(requirements.data.BlobIndex[0].blob.data)
    requirements.data.BlobIndex[0].blob.length = len(requirements.data.BlobIndex[0].blob.bytes) + 8
    requirements.bytes = macho_cs.Entitlements.build(requirements.data)
    requirements.length = len(requirements.bytes) + 8
    requirements_data = macho_cs.Blob_.build(requirements)
    print hashlib.sha1(requirements_data).hexdigest()
    #print hexdump(requirements_data)
    print

    print "code directory:"
    cd = get_codesig_blob(codesig_cons, 'CSMAGIC_CODEDIRECTORY')
    print cd
    cd.data.hashes[0] = hashlib.sha1(entitlements_data).digest()
    cd.data.hashes[2] = hashlib.sha1(open("../resigned/NativeIOSTestApp.app/_CodeSignature/CodeResources", "rb").read()).digest()
    cd.data.hashes[3] = hashlib.sha1(requirements_data).digest()
    cd.data.teamID = "JWKXD469L2"
    cd.bytes = macho_cs.CodeDirectory.build(cd.data)
    cd_data = macho_cs.Blob_.build(cd)
    print len(cd_data)
    #open("cdrip", "wb").write(cd_data)
    print "CDHash:", hashlib.sha1(cd_data).hexdigest()
    print

    print "sig:"
    sigwrapper = get_codesig_blob(codesig_cons, 'CSMAGIC_BLOBWRAPPER')
    #print_parsed_asn1(sigwrapper.data.data.value)
    #open("sigrip.der", "wb").write(sigwrapper.data.data.value)
    sig = sign(cd_data,
               signer_cert_file,
               signer_key_file,
               cert_file)
    oldsig = sigwrapper.bytes.value
    print "sig len:", len(sig)
    print "old sig len:", len(oldsig)
    #open("my_sigrip.der", "wb").write(sig)
    #print hexdump(oldsig)
    sigwrapper.data = construct.Container(data=sig)
    #print_parsed_asn1(sig)
    #sigwrapper.data = construct.Container(data="hahaha")
    sigwrapper.length = len(sigwrapper.data.data) + 8
    sigwrapper.bytes = sigwrapper.data.data
    print len(sigwrapper.bytes)
    #print hexdump(sigwrapper.bytes)
    print

    superblob = macho_cs.SuperBlob.build(codesig_cons.data)
    codesig_cons.length = len(superblob) + 8
    codesig_cons.bytes = superblob

    return codesig_cons


def main():
    parser = OptionParser()
    options, args = parser.parse_args()
    filename = args[0]
    entitlements_file = "Entitlements.plist"

    f = open(filename, "rb")
    m = macho.MachoFile.parse(f.read())
    base_offset = 0
    m2 = m.data
    print m.data.keys()
    print type(m)
    print type(m.data)
    if 'FatArch' in m.data:
        base_offset = 0x1000
        m2 = m.data.FatArch[0].MachO

    cmds = {}
    for cmd in m2.commands:
        name = cmd.cmd
        cmds[name] = cmd

    print cmds['LC_DYLIB_CODE_SIGN_DRS'].data.blob

    if 'LC_CODE_SIGNATURE' in cmds:
        codesig_offset = base_offset + cmds['LC_CODE_SIGNATURE'].data.dataoff
        f.seek(codesig_offset)
        codesig_data = f.read(cmds['LC_CODE_SIGNATURE'].data.datasize)
        #print len(codesig_data)
        #print hexdump(codesig_data)
        codesig_cons = macho_cs.Blob.parse(codesig_data)
    else:
        codesig_data = ""
        codesig_cons = make_basic_codesig(entitlements_file)
        cmd_data = construct.Container(dataoff=f.tell(),
                                       datasize=10000)
        cmd = construct.Container(cmd='LC_CODE_SIGNATURE',
                                  cmdsize=16,
                                  data=cmd_data,
                                  bytes=macho.CodeSigRef.build(cmd_data))
        m2.commands.append(cmd)
        m2.ncmds += 1
        m2.sizeofcmds += len(macho.LoadCommand.build(cmd))
        cmds['LC_CODE_SIGNATURE'] = cmd
        print codesig_cons

    print codesig_cons

    # print hashes
    cd = codesig_cons.data.BlobIndex[0].blob.data
    print cd
    end_offset = base_offset + cd.codeLimit
    start_offset = ((end_offset + 0xfff) & ~0xfff) - (cd.nCodeSlots * 0x1000)
    for i in xrange(cd.nSpecialSlots):
        expected = cd.hashes[i]
        print "special exp=%s" % expected.encode('hex')
    for i in xrange(cd.nCodeSlots):
        expected = cd.hashes[cd.nSpecialSlots + i]
        f.seek(start_offset + 0x1000 * i)
        actual_data = f.read(min(0x1000, end_offset - f.tell()))
        actual = hashlib.sha1(actual_data).digest()
        print '[%s] exp=%s act=%s' % (
            ('bad', 'ok ')[expected == actual],
            expected.encode('hex'),
            actual.encode('hex')
        )

    new_codesig_cons = resign_cons(codesig_cons,
                                   entitlements_file,
                                   '~/devcert.pem',
                                   '~/devkey.p12',
                                   '~/applecerts.pem')
    new_codesig_data = macho_cs.Blob.build(new_codesig_cons)
    print "old len:", len(codesig_data)
    print "new len:", len(new_codesig_data)

    new_codesig_data += "\x00" * (len(codesig_data) - len(new_codesig_data))
    print "padded len:", len(new_codesig_data)
    print "----"
    #print hexdump(new_codesig_data)
    #assert new_codesig_data != codesig_data

    cmd = cmds['LC_CODE_SIGNATURE']
    cmd.data.datasize = len(new_codesig_data)
    cmd.bytes = macho.CodeSigRef.build(cmd.data)
    print m2

    f3 = open("foo", "wb")
    f3.write(macho.MachoFile.build(m))
    f.seek(f3.tell())  # FIXME -- really want original f header size, not new m length
    f3.write(f.read(cmds['LC_CODE_SIGNATURE'].data.dataoff - f3.tell()))
    print "writing codesig to", hex(cmds['LC_CODE_SIGNATURE'].data.dataoff)
    f3.seek(cmds['LC_CODE_SIGNATURE'].data.dataoff)
    f3.write(new_codesig_data)
    f3.close()


if __name__ == '__main__':
    main()
